"""Реальный PostgreSQL в PGlite: права, RLS, gate и CAS без удалённой БД."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_watchdog_closed_roles_atomic_gate_and_cron(tmp_path):
    module = os.environ.get('CAROUSEL_COVER_TEST_PGLITE', '/private/tmp/carousel-cover-dbtest/node_modules/@electric-sql/pglite/dist/index.js')
    if not shutil.which('node') or not Path(module).is_file():
        pytest.skip('Локальный PGlite недоступен')
    migration = Path(__file__).resolve().parents[2] / 'supabase/migrations/20261024093600_carousel_cover_watchdog.sql'
    script = tmp_path / 'watchdog-sql.mjs'
    script.write_text(r'''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const {PGlite} = await import(pathToFileURL(process.argv[2]));
const db = new PGlite();
function assert(value, message) { if (!value) throw new Error(message); }
await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create schema cron;
  create table cron.job(jobid bigint generated always as identity, jobname text, schedule text, command text, active boolean default true);
  create function cron.schedule(text,text,text) returns bigint language sql as $$
    insert into cron.job(jobname,schedule,command) values($1,$2,$3) returning jobid
  $$;
  create function cron.alter_job(job_id bigint, active boolean) returns void language sql as $$
    update cron.job set active=$2 where jobid=$1
  $$;
`);
await db.exec(readFileSync(process.argv[3], 'utf8'));
const query = (sql, args=[]) => db.query(sql,args);
const rpc = async (sql,args=[]) => (await query(sql,args)).rows[0].r;
const state = () => rpc('select public.carousel_cover_background_status() r');
const claim = () => rpc('select public.carousel_cover_background_claim_tick() r');
const finish = (token,reason='dispatch_sent',refs={},summary={pending:2}) => rpc(
  'select public.carousel_cover_background_finish_tick($1,$2,$3,$4) r',[token,reason,refs,summary]);
const cron = (await query('select * from cron.job')).rows;
assert(cron.length===1 && cron[0].active===false && cron[0].schedule==='*/5 * * * *','свой cron не выключен');
assert(cron[0].command.includes('https://reelscribe-ai.vercel.app/api/radar/carousel-cover-background/tick'),'нефиксированный tick URL');
assert(!/token|authorization|secret|api_key/i.test(cron[0].command),'секрет в cron');
for (const role of ['anon','authenticated']) {
  await db.exec('set role '+role);
  for (const sql of ['select * from public.carousel_cover_background_control',
      'select public.carousel_cover_background_status()', 'select public.carousel_cover_background_claim_tick()',
      "select public.carousel_cover_background_finish_tick('00000000-0000-0000-0000-000000000000','empty','{}','{}')"]) {
    let denied = false;
    try { await query(sql); } catch (e) { denied=e.message.includes('permission denied'); }
    assert(denied, role+' получил доступ: '+sql);
  }
  await db.exec('reset role');
}
await db.exec('grant select on public.carousel_cover_background_control to anon; set role anon');
assert((await query('select * from public.carousel_cover_background_control')).rows.length===0,'RLS открыл контроллер');
await db.exec('reset role; revoke select on public.carousel_cover_background_control from anon; set role service_role');
assert((await state()).enabled===false && !(await claim()).claimed,'контроллер включился до приёмки');
await db.exec('update public.carousel_cover_background_control set enabled=true where singleton');
const claims = await Promise.all([claim(),claim(),claim()]);
assert(claims.filter(x=>x.claimed).length===1,'дублирован dispatch gate');
const first = claims.find(x=>x.claimed).token;
const afterClaim = await state();
assert(!Object.hasOwn(afterClaim,'gate_token'),'status вернул gate token');
assert(new Date(afterClaim.not_before)-new Date(afterClaim.heartbeat)===300000,'gate не равен пяти минутам');
assert(afterClaim.last_dispatch_at===null,'claim сделал вид, что dispatch отправлен');
const refs={archive7d:'a'.repeat(40),archive14d:'b'.repeat(40),live:'c'.repeat(40)};
assert((await finish(first,'dispatch_sent',refs,{pending:2,unknown:1,remaining:3})).recorded,'finish не записан');
assert((await state()).last_dispatch_at!==null && (await state()).last_seen_source_refs.live===refs.live,'нет heartbeat/refs');
assert(!(await claim()).claimed,'gate снят после finish и разрешил повторный dispatch');
assert(!(await finish(first,'empty')).recorded,'старый token принят повторно');
await db.exec("update public.carousel_cover_background_control set not_before=clock_timestamp()-interval '1 second' where singleton");
const second = await claim();
assert(second.claimed && second.token!==first,'новый gate не выдан');
assert(!(await finish(first,'empty')).recorded,'поздний finish перезаписал новую проверку');
for (const [refsArg,summary] of [[{arbitrary:'x'},{}],[{}, {ids: 1}],[{}, {pending:-1}],[{}, {pending:1.5}]]) {
  let denied=false;
  try { await finish(second.token,'empty',refsArg,summary); } catch { denied=true; }
  assert(denied,'некорректная сводка принята');
}
await finish(second.token,'quota',{},{});
await db.exec('update public.carousel_cover_background_control set enabled=false,not_before=null where singleton');
assert(!(await claim()).claimed,'disabled не блокирует gate');
await db.exec('reset role');
const funcs=(await query("select prosecdef from pg_proc where proname like 'carousel_cover_background_%'")).rows;
assert(funcs.length===3 && funcs.every(x=>x.prosecdef===false),'ненужный security definer');
await db.close();
console.log('watchdog SQL ok');
''', encoding='utf-8')
    result = subprocess.run(['node', str(script), module, str(migration)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-5000:]
    assert 'watchdog SQL ok' in result.stdout
