// Изолированный прогон PostgreSQL/WASM; PGlite устанавливается только во временную папку.
// Запуск: PGLITE_MODULE=/путь/к/pglite/dist/index.js node supabase/tests/carousel_caption_translation.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'

const { PGlite } = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite')
const db = new PGlite()
const migration = fileURLToPath(new URL('../migrations/20261020124407_carousel_caption_translations.sql', import.meta.url))
const hash = 'a'.repeat(64)
const otherHash = 'b'.repeat(64)
const checks = []
const reserve = async (id, digest = hash, token = randomUUID()) => {
  const { rows } = await db.query('select public.reserve_carousel_caption_translation($1,$2,$3) as value', [id, digest, token])
  return { ...rows[0].value, token }
}
const finish = async (id, token, digest = hash, outcome = 'failed') => {
  const { rows } = await db.query('select public.finish_carousel_caption_translation($1,$2,$3,$4,$5,$6) as value',
    [id, digest, token, outcome === 'ready' ? 'Проверенный русский перевод для читателя.' : null, 'en', outcome])
  return rows[0].value
}
const reset = async () => db.exec('reset role; truncate public.carousel_caption_translations,public.carousel_caption_translation_attempts; set role service_role;')

try {
  await db.exec('create role anon; create role authenticated; create role service_role bypassrls;')
  await db.exec(readFileSync(migration, 'utf8'))
  checks.push('migration_syntax')
  const privileges = await db.query(`select
    has_table_privilege('anon','public.carousel_caption_translations','SELECT') as anon_read,
    has_table_privilege('authenticated','public.carousel_caption_translations','INSERT') as authenticated_write,
    has_function_privilege('anon','public.reserve_carousel_caption_translation(text,text,uuid)','EXECUTE') as anon_rpc,
    has_function_privilege('authenticated','public.finish_carousel_caption_translation(text,text,uuid,text,text,text)','EXECUTE') as authenticated_rpc,
    (select bool_and(relrowsecurity) from pg_class where oid in
      ('public.carousel_caption_translations'::regclass,'public.carousel_caption_translation_attempts'::regclass)) as rls`)
  assert.deepEqual(privileges.rows[0], { anon_read: false, authenticated_write: false, anon_rpc: false, authenticated_rpc: false, rls: true })
  await db.exec('set role anon;')
  await assert.rejects(() => reserve('1'), /permission denied/)
  await db.exec('reset role; set role service_role;')
  checks.push('roles_rls_rpc_revoke')

  const one = await reserve('1')
  assert.equal(one.state, 'reserved')
  assert.equal((await reserve('1')).state, 'in_progress')
  assert.equal((await reserve('2')).state, 'reserved')
  const limited = await reserve('3')
  assert.equal(limited.state, 'limited')
  assert.ok(limited.retry_after > 0 && limited.retry_after <= 900)
  assert.equal((await finish('1', one.token, hash, 'ready')).state, 'ready')
  assert.equal((await reserve('1')).state, 'ready')
  assert.equal((await db.query('select count(*)::int as total from public.carousel_caption_translation_attempts')).rows[0].total, 2)
  assert.equal((await reserve('3')).state, 'reserved')
  checks.push('singleflight_two_leases_cache_free')

  await reset()
  for (let n = 0; n < 12; n++) {
    const attempt = await reserve(String(n + 1))
    assert.equal(attempt.state, 'reserved')
    assert.equal((await finish(String(n + 1), attempt.token)).state, 'finished')
  }
  const hourly = await reserve('99')
  assert.equal(hourly.state, 'limited')
  assert.ok(hourly.retry_after > 0 && hourly.retry_after <= 3600)
  await db.exec("update public.carousel_caption_translation_attempts set started_at=started_at-interval '61 minutes',lease_until=lease_until-interval '61 minutes';")
  assert.equal((await reserve('99')).state, 'reserved')
  checks.push('twelve_starts_sliding_hour_no_refund')

  await reset()
  const old = await reserve('1')
  await db.exec("update public.carousel_caption_translation_attempts set started_at=started_at-interval '16 minutes',lease_until=lease_until-interval '16 minutes';")
  const fresh = await reserve('1')
  assert.equal(fresh.state, 'reserved')
  assert.notEqual(old.token, fresh.token)
  assert.equal((await finish('1', old.token, hash, 'ready')).state, 'stale')
  assert.equal((await finish('2', fresh.token, hash, 'ready')).state, 'stale')
  assert.equal((await finish('1', fresh.token, otherHash, 'ready')).state, 'stale')
  assert.equal((await db.query('select count(*)::int as total from public.carousel_caption_translations')).rows[0].total, 0)
  assert.equal((await finish('1', fresh.token, hash, 'ready')).state, 'ready')
  assert.equal((await finish('1', old.token, hash, 'ready')).state, 'stale')
  assert.equal((await db.query('select count(*)::int as total from public.carousel_caption_translation_attempts')).rows[0].total, 2)
  assert.equal((await reserve('1', otherHash)).state, 'reserved')
  checks.push('expiry_fencing_owner_identity_hash_and_ledger')

  await reset()
  const results = await Promise.all(Array.from({ length: 8 }, (_, i) => reserve(String(i + 1))))
  assert.equal(results.filter(row => row.state === 'reserved').length, 2)
  assert.equal(results.filter(row => row.state === 'limited').length, 6)
  checks.push('queued_calls_limit')
  console.log(JSON.stringify({ passed: checks, concurrency_limit: 'PGlite выполняет запросы одного соединения последовательно; настоящие независимые PostgreSQL-транзакции здесь не проверены.' }, null, 2))
} finally {
  await db.close()
}
