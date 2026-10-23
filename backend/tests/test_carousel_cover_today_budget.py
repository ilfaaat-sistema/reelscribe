"""Разовый московский лимит: исполняемый PostgreSQL в локальном PGlite."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_carousel_cover_sql import PGLITE_PATHS

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'supabase/migrations/20261020123535_carousel_cover_translations.sql'
MIGRATION = ROOT / 'supabase/migrations/20261023095816_carousel_cover_today_budget.sql'


def test_budget_clock_is_sampled_once_after_reservation_lock():
    sql = MIGRATION.read_text()
    state, reserve = sql.split('create or replace function public.reserve_carousel_cover(', 1)
    assert state.count('clock_timestamp()') == 1
    assert reserve.count('clock_timestamp()') == 1
    assert reserve.index('where singleton for update;') < reserve.index('v_now := clock_timestamp();')
    for body in (state, reserve):
        assert 'public.carousel_cover_daily_limit(v_now)' in body
        assert 'public.carousel_cover_day_window(v_now)' in body
    # Внешний вызов не может подменить часы или передать свой лимит.
    assert 'p_at timestamptz' in sql
    assert 'p_limit' not in sql
    assert 'p_now' not in sql


def test_one_day_budget_and_existing_guards_in_postgres(tmp_path):
    module = os.environ.get('CAROUSEL_COVER_TEST_PGLITE') or next(
        (path for path in PGLITE_PATHS if Path(path).is_file()), '')
    if not shutil.which('node') or not Path(module).is_file():
        pytest.skip('Локальный PGlite недоступен')
    script = tmp_path / 'cover-today-budget.mjs'
    script.write_text(r'''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const {PGlite} = await import(pathToFileURL(process.argv[2]));
const db = new PGlite();
const assert = (condition,message) => { if (!condition) throw new Error(message); };
const query = (sql,args=[]) => db.query(sql,args);
const rpc = async (sql,args=[]) => (await query(sql,args)).rows[0].r;
await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
await db.exec(readFileSync(process.argv[3],'utf8'));
// Только локальная фикстура: фиксированные часы без нового production RPC.
await db.exec(`create function public.cover_test_now() returns timestamptz language sql volatile as $$
  select current_setting('cover_test.at')::timestamptz $$;
  update public.carousel_cover_budget set attempts=72,mode='live';
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,slot_held,created_at)
  values('1','v1','error',gen_random_uuid(),true,'2026-10-22T20:00:00Z'),
        ('2','v1','running',gen_random_uuid(),true,'2026-10-22T20:00:00Z');`);
const snapshot = () => rpc(`select jsonb_build_object('budget',(select to_jsonb(b) from public.carousel_cover_budget b),
  'rows',(select jsonb_agg(to_jsonb(t) order by media_id) from public.carousel_cover_translations t)) r`);
const definitions = () => rpc(`select jsonb_object_agg(proname,pg_get_functiondef(oid)) r from pg_proc where
  pronamespace='public'::regnamespace and proname in ('finish_carousel_cover','unavailable_carousel_cover','set_carousel_cover_mode','carousel_cover_day_window')`);
const before = await snapshot();
const beforeFunctions = await definitions();
const migration = readFileSync(process.argv[4],'utf8');
await db.exec(migration);
assert(JSON.stringify(await snapshot())===JSON.stringify(before),'миграция изменила lifetime, строки или слоты');
assert(JSON.stringify(await definitions())===JSON.stringify(beforeFunctions),'миграция изменила посторонние RPC');
// Подмена только чтения часов в disposable БД; сам helper исполняется без изменений.
await db.exec(migration.slice(migration.indexOf('create or replace function')).replaceAll('clock_timestamp()','public.cover_test_now()'));
const at = value => query("select set_config('cover_test.at',$1,false)",[value]);
const state = () => rpc('select public.carousel_cover_budget_state() r');
const mode = value => rpc('select public.set_carousel_cover_mode($1) r',[value]);
const reserve = (id,version='v1') => rpc('select public.reserve_carousel_cover($1,$2,$3,$4) r',[id,version,'model','a'.repeat(64)]);
const finish = (id,token,uncertain=false) => rpc('select public.finish_carousel_cover($1,$2,$3,$4,$5,$6,$7,$8,$9) r',
  [id,'v1',token,uncertain?'error':'ready','Title','Заголовок',{},12,uncertain]);
await at('2026-10-23T20:59:59Z');
for (const role of ['anon','authenticated']) {
  await db.exec('set role '+role);
  for (const sql of ["select public.carousel_cover_daily_limit('2026-10-23T12:00Z')",'select public.carousel_cover_budget_state()',
    "select public.reserve_carousel_cover('100','v1','m',repeat('a',64))"]) {
    let rejected=false;
    try { await query(sql); } catch (e) { rejected=e.message.includes('permission denied'); }
    assert(rejected,role+' получил привилегированный RPC');
  }
  await db.exec('reset role');
}
await db.exec("set time zone 'Pacific/Honolulu'; set role service_role;");
for (const [time,limit] of [
  ['2026-10-22T20:59:59Z',500], ['2026-10-22T21:00:00Z',2500],
  ['2026-10-23T20:59:59Z',2500], ['2026-10-23T20:59:59.999999Z',2500],
  ['2026-10-23T21:00:00Z',500], ['2026-10-24T12:00:00Z',500],
  ['2027-10-23T12:00:00Z',500]]) {
  assert(await rpc('select public.carousel_cover_daily_limit($1) r',[time])===limit,'неверный лимит '+time);
  await at(time);
  assert((await state()).daily_limit===limit,'state неверно выбрал лимит '+time);
}
await at('2026-10-23T12:00:00Z');
assert((await reserve('3')).status==='busy','миграция освободила два старых слота');
assert((await state()).attempts===72,'busy потратил lifetime');
// Наполняем 2499 уже оплаченных попыток, не вызывая модель.
await db.exec(`reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=2700;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
  select n::text,'v1','no_text',gen_random_uuid(),'2026-10-23T00:00Z' from generate_series(1,2499) n;
  set role service_role;`);
assert((await state()).daily_attempts===2499 && (await state()).status==='available','2499 преждевременно закрыли бюджет');
const last=await reserve('2500');
assert(last.claim_token,'2500-я попытка не выдана');
assert((await reserve('2501')).status==='limit_reached','выдана 2501-я попытка');
assert((await state()).attempts===2701 && (await state()).daily_attempts===2500,'неверный lifetime/дневной учёт');
let wrong=false;
try { await finish('2500','00000000-0000-0000-0000-000000000000'); } catch(e) { wrong=e.message.includes('cover_claim_mismatch'); }
assert(wrong,'сломана защита claim token');
await finish('2500',last.claim_token);
const exhausted=await state();
assert(exhausted.retry_after==='2026-10-23T21:00:00Z','неверная дата сброса');
assert((await reserve('2500')).status==='ready','бюджет закрыл кэш');
await rpc("select public.unavailable_carousel_cover('9999','v1') r");
assert((await state()).attempts===2701 && (await state()).daily_attempts===2500,'кэш или недоступная картинка списали попытку');
await mode('pilot');
assert((await reserve('2501')).status==='limit_reached' && (await state()).retry_after===null,'повышение сломало lifetime пилота');
await mode('live');
assert((await state()).daily_attempts===2500,'смена режима сбросила дневные записи');
// Одна транзакция пересекает полночь: решение и created_at используют новые фактические часы.
await db.exec('begin');
await at('2026-10-23T21:00:00Z');
const midnight=await reserve('2501');
assert(midnight.claim_token,'полночь не открыла новые сутки');
assert((await state()).daily_limit===500 && (await state()).daily_attempts===1,'после полуночи не восстановлен лимит 500');
assert((await query("select created_at='2026-10-23T21:00:00Z'::timestamptz as fresh from public.carousel_cover_translations where media_id='2501'")).rows[0].fresh,'created_at использует старые часы');
await db.exec('commit');
await finish('2501',midnight.claim_token,true);
const active=await reserve('2502');
assert(active.claim_token && (await reserve('2503')).status==='busy','потерян предел двух слотов');
await at('2026-10-24T21:00:00Z');
assert((await reserve('2503')).status==='busy','новый день освободил слоты');
assert((await state()).attempts===2703 && (await state()).daily_attempts===0,'полночь сбросила lifetime/дневной подсчёт');
assert((await reserve('2501')).status==='error','неопределённый исход запущен повторно');
assert((await query('select count(*)::int n from public.carousel_cover_translations')).rows[0].n===2503,'дневной сброс удалил записи');
// Следующие сутки ограничены именно 500; смена режима сохраняет lifetime.
await db.exec(`reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=3202;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
  select n::text,'v1','no_text',gen_random_uuid(),'2026-10-25T00:00Z' from generate_series(1,499) n;
  set role service_role;`);
const normal=await reserve('500');
assert(normal.claim_token && (await reserve('501')).status==='limit_reached','после временного повышения действует не 500');
assert((await state()).attempts===3203,'обычный день сбросил lifetime');
// Ровно двадцать пилотных попыток независимо от временного live-предела.
await db.exec("reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=19,mode='pilot'; set role service_role;");
await at('2026-10-23T12:00:00Z');
assert((await reserve('20')).claim_token,'20-я пилотная попытка не выдана');
const pilot=await reserve('21');
assert(pilot.status==='limit_reached' && pilot.retry_after===null && (await state()).attempts===20,'пилот вышел за 20');
await db.close();
console.log('Проверены 23.10.2026 МСК: 2500/2501, автооткат 500, UTC-границы, роли, lifetime, кэш, 20 пилота, два слота и fencing. PGlite последовательно исполняет очередь; реальное ожидание lock проверяется структурой функции.');
''')
    result = subprocess.run(['node', str(script), module, str(BASE), str(MIGRATION)],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
