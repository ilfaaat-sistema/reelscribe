// Локальная приёмка SQL на PostgreSQL/WASM: время подменено только в изолированном тесте.
// Запуск: PGLITE_MODULE=/путь/к/pglite/dist/index.js node supabase/tests/carousel_caption_daily_budget.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

const { PGlite } = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite')
const db = new PGlite()
const hash = 'a'.repeat(64)
const otherHash = 'b'.repeat(64)
const checks = []
const now = async value => db.query("select set_config('caption_test.now',$1,false)", [value])
const queryValue = async (sql, args = []) => (await db.query(sql, args)).rows[0].value
const reserve = async (id, { auto = false, digest = hash, token = randomUUID() } = {}) => ({
  ...await queryValue(`select public.reserve_carousel_caption_translation${auto ? '_auto' : ''}($1,$2,$3) as value`, [id, digest, token]), token,
})
const finish = async (id, token, { digest = hash, outcome = 'failed' } = {}) => queryValue(
  'select public.finish_carousel_caption_translation($1,$2,$3,$4,$5,$6) as value',
  [id, digest, token, outcome === 'ready' ? 'Проверенный русский перевод для читателя.' : null, 'en', outcome])
const total = async () => queryValue('select count(*)::int as value from public.carousel_caption_translation_attempts')
const reset = async () => {
  await db.exec('reset role; truncate public.carousel_caption_translations,public.carousel_caption_translation_attempts; set role service_role;')
  await now('2026-10-23T09:00:00Z')
}

try {
  await db.exec('create role anon; create role authenticated; create role service_role bypassrls;')
  await db.exec(`create function public.caption_test_clock() returns timestamptz language sql as $$
    select current_setting('caption_test.now')::timestamptz $$;`)
  await now('2026-10-23T09:00:00Z')
  for (const name of ['20261020124407_carousel_caption_translations.sql', '20261023083524_carousel_caption_daily_budget.sql']) {
    const sql = readFileSync(new URL(`../migrations/${name}`, import.meta.url), 'utf8')
    await db.exec(sql.replaceAll('clock_timestamp()', 'public.caption_test_clock()'))
  }
  checks.push('base_and_new_migration_syntax')
  const permissions = await db.query(`select
    has_function_privilege('anon','public.reserve_carousel_caption_translation_auto(text,text,uuid)','EXECUTE') as anon_auto,
    has_function_privilege('authenticated','public.reserve_carousel_caption_translation_auto(text,text,uuid)','EXECUTE') as authenticated_auto,
    has_function_privilege('service_role','public.reserve_carousel_caption_translation_auto(text,text,uuid)','EXECUTE') as service_auto,
    has_function_privilege('anon','public.reserve_carousel_caption_translation(text,text,uuid)','EXECUTE') as anon_manual,
    has_table_privilege('anon','public.carousel_caption_translation_attempts','SELECT') as anon_attempt,
    (select bool_and(relrowsecurity) from pg_class where oid in
      ('public.carousel_caption_translations'::regclass,'public.carousel_caption_translation_attempts'::regclass)) as rls`)
  assert.deepEqual(permissions.rows[0], { anon_auto: false, authenticated_auto: false, service_auto: true, anon_manual: false, anon_attempt: false, rls: true })
  await db.exec('set role anon;')
  await assert.rejects(() => reserve('1', { auto: true }), /permission denied/)
  await db.exec('reset role; set role service_role;')
  await assert.rejects(() => reserve('bad-id', { auto: true }), /invalid_translation_request/)
  checks.push('closed_auto_rpc_validation_and_preserved_permissions')

  const one = await reserve('1', { auto: true })
  assert.equal(one.state, 'reserved')
  assert.equal((await reserve('1', { auto: true })).state, 'in_progress')
  assert.equal((await reserve('2')).state, 'reserved')
  const limited = await reserve('3', { auto: true })
  assert.equal(limited.state, 'limited')
  assert.equal(limited.retry_after, 900)
  await finish('1', one.token, { outcome: 'ready' })
  assert.equal((await reserve('1', { auto: true })).state, 'ready')
  assert.equal(await total(), 2)
  assert.equal((await reserve('3', { auto: true })).state, 'reserved')
  checks.push('singleflight_two_global_leases_and_free_ready_cache')

  await reset()
  for (let i = 0; i < 500; i++) {
    const id = String(i + 1)
    const attempt = await reserve(id, { auto: true })
    assert.equal(attempt.state, 'reserved')
    if (i > 0) {
      assert.equal((await finish(id, attempt.token, { outcome: ['failed', 'quota', 'source_changed'][i % 3] })).state, 'finished')
    }
  }
  const daily = await reserve('999', { auto: true })
  assert.equal(daily.state, 'limited')
  assert.equal(daily.retry_after, 12 * 3600)
  assert.equal(await total(), 500)
  const moscow = await db.query("select started_at >= '2026-10-22T21:00:00Z'::timestamptz as in_day from public.carousel_caption_translation_attempts limit 1")
  assert.equal(moscow.rows[0].in_day, true)
  await now('2026-10-23T09:16:00Z')
  assert.equal((await reserve('999', { auto: true })).state, 'limited')
  assert.equal(await total(), 500)
  checks.push('500_starts_per_moscow_day_definitive_errors_and_expired_unknown_no_refund')

  await now('2026-10-23T20:59:59.200Z')
  assert.equal((await reserve('999')).retry_after, 1)
  await db.exec("set time zone 'America/New_York';")
  await now('2026-10-23T21:00:00Z')
  const nextDay = await reserve('999', { auto: true })
  assert.equal(nextDay.state, 'reserved')
  assert.equal(await total(), 501)
  assert.equal((await reserve('1', { auto: true })).state, 'previous_attempt')
  checks.push('moscow_midnight_reset_independent_of_session_timezone_and_permanent_history')

  await reset()
  await now('2026-10-23T20:55:00Z')
  assert.equal((await reserve('1')).state, 'reserved')
  assert.equal((await reserve('2')).state, 'reserved')
  await now('2026-10-23T21:00:00Z')
  const crossDay = await reserve('3', { auto: true })
  assert.equal(crossDay.state, 'limited')
  assert.equal(crossDay.retry_after, 600)
  checks.push('leases_stay_global_across_day_boundary')

  await reset()
  const old = await reserve('1')
  await now('2026-10-25T09:00:00Z')
  assert.equal((await reserve('1', { auto: true })).state, 'previous_attempt')
  assert.equal((await reserve('2', { auto: true })).state, 'reserved')
  assert.equal(await total(), 2)
  const fresh = await reserve('1')
  assert.equal(fresh.state, 'reserved')
  assert.equal((await finish('1', old.token, { outcome: 'ready' })).state, 'stale')
  assert.equal((await finish('2', fresh.token, { outcome: 'ready' })).state, 'stale')
  assert.equal((await finish('1', fresh.token, { digest: otherHash, outcome: 'ready' })).state, 'stale')
  assert.equal((await finish('1', fresh.token, { outcome: 'ready' })).state, 'ready')
  assert.equal((await reserve('1', { auto: true })).state, 'ready')
  await db.exec("delete from public.carousel_caption_translations where instagram_media_id='1';")
  assert.equal((await reserve('1', { auto: true })).state, 'previous_attempt')
  checks.push('manual_retry_kept_auto_history_never_deleted_fencing_and_missing_cache_guard')

  await reset()
  for (const [i, outcome] of ['failed', 'quota', 'source_changed'].entries()) {
    const id = String(i + 1)
    // Сценарий гонки: worker видел none, ручная попытка успела завершиться до его POST.
    const manual = await reserve(id)
    await finish(id, manual.token, { outcome })
    assert.equal((await reserve(id, { auto: true })).state, 'previous_attempt')
  }
  assert.equal(await total(), 3)
  const newSource = await reserve('1', { auto: true, digest: otherHash })
  assert.equal(newSource.state, 'reserved')
  const simultaneous = await Promise.all(Array.from({ length: 6 }, () => reserve('9', { auto: true })))
  assert.equal(simultaneous.filter(row => row.state === 'reserved').length, 1)
  assert.equal(simultaneous.filter(row => row.state === 'in_progress').length, 5)
  checks.push('manual_race_history_guard_source_hash_isolation_and_queued_auto_singleflight')

  console.log(JSON.stringify({ passed: checks, clock: 'Только тестовая замена clock_timestamp на фиксируемую SQL-функцию.', concurrency_limit: 'PGlite использует одно соединение; независимые PostgreSQL-транзакции здесь не проверены.' }, null, 2))
} finally {
  await db.close()
}
