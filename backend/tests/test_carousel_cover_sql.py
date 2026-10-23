"""SQL в локальном PGlite; удалённая БД и модель не вызываются."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PGLITE_PATHS = (
    '/private/tmp/reels-authors-sql-test/node_modules/@electric-sql/pglite/dist/index.js',
    '/private/tmp/residents-export-sql/node_modules/@electric-sql/pglite/dist/index.js',
    '/private/tmp/resident-report-dbqa/node_modules/@electric-sql/pglite/dist/index.js',
)


def test_real_postgres_budget_claims_and_permissions(tmp_path):
    module = os.environ.get('CAROUSEL_COVER_TEST_PGLITE') or next(
        (path for path in PGLITE_PATHS if Path(path).is_file()), '')
    if not shutil.which('node') or not Path(module).is_file():
        pytest.skip('Локальный PGlite недоступен')
    migration = Path(__file__).resolve().parents[2] / 'supabase/migrations/20261020123535_carousel_cover_translations.sql'
    script = tmp_path / 'cover-sql.mjs'
    script.write_text(r'''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const {PGlite} = await import(pathToFileURL(process.argv[2]));
const db = new PGlite();
function assert(value, message) { if (!value) throw new Error(message); }
await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
await db.exec(readFileSync(process.argv[3], 'utf8'));
const query = (sql, args=[]) => db.query(sql,args);
const rpc = async (sql,args=[]) => (await query(sql,args)).rows[0].r;
const state = () => rpc('select public.carousel_cover_budget_state() r');
const mode = (value) => rpc('select public.set_carousel_cover_mode($1) r',[value]);
const reserve = (id, version='v1') => rpc('select public.reserve_carousel_cover($1,$2,$3,$4) r', [id,version,'model','a'.repeat(64)]);
const finish = (id, token, uncertain=false, version='v1') => rpc('select public.finish_carousel_cover($1,$2,$3,$4,$5,$6,$7,$8,$9) r', [id,version,token,uncertain?'error':'ready','Title','Заголовок',{},12,uncertain]);
for (const role of ['anon','authenticated']) {
  await db.exec('set role '+role);
  for (const sql of ['select * from public.carousel_cover_translations','select * from public.carousel_cover_budget',
      "select public.reserve_carousel_cover('1','v1','m',repeat('a',64))",
      "select public.unavailable_carousel_cover('1','v1')",
      "select public.finish_carousel_cover('1','v1','00000000-0000-0000-0000-000000000000','error',null,null,'{}',1,false)",
      "select public.set_carousel_cover_mode('live')", 'select public.carousel_cover_budget_state()',
      "select * from public.carousel_cover_day_window('2026-10-23T21:00:00Z')"]) {
    let rejected = false;
    try { await query(sql); } catch (e) { rejected = e.message.includes('permission denied'); }
    assert(rejected, role+' получил запрещённый доступ: '+sql);
  }
  await db.exec('reset role');
}
// Даже при случайном SELECT grant RLS не открывает содержимое посторонним ролям.
await db.exec("insert into public.carousel_cover_translations(media_id,prompt_version,status) values('555','rls','no_text'); grant select on public.carousel_cover_translations to anon; set role anon;");
assert((await query('select * from public.carousel_cover_translations')).rows.length===0,'RLS открыл строки');
await db.exec("reset role; revoke select on public.carousel_cover_translations from anon; delete from public.carousel_cover_translations where media_id='555'; set role service_role;");
// Время брони не должно оставаться временем начала ожидающей транзакции.
await db.exec('begin');
await new Promise(resolve=>setTimeout(resolve,25));
await reserve('777');
assert((await query("select created_at > transaction_timestamp() as fresh from public.carousel_cover_translations where media_id='777'")).rows[0].fresh,'бронь использует устаревшее время транзакции');
await db.exec('rollback');
const initial = await state();
assert(initial.mode==='pilot' && initial.attempts===0 && initial.daily_attempts===0,'неверный режим по умолчанию');
assert(initial.pilot_limit===20 && initial.daily_limit===500 && initial.retry_after===null,'неверные пределы');
let invalidMode=false;
try { await mode('unlimited'); } catch (e) { invalidMode=e.message.includes('invalid_cover_mode'); }
assert(invalidMode && (await state()).mode==='pilot','невалидный режим принят');
const [a,b,c] = await Promise.all([reserve('1'), reserve('2'), reserve('3')]);
assert(a.claim_token && b.claim_token && c.status==='busy','нарушен предел двух слотов');
const same = await reserve('1');
assert(same.status==='running' && !same.claim_token,'повтор получил право вызова модели');
let wrong = false;
try { await finish('1',b.claim_token); } catch (e) { wrong = e.message.includes('cover_claim_mismatch'); }
assert(wrong,'чужой token принят');
await finish('1',a.claim_token);
await finish('2',b.claim_token);
for (let n=3;n<=20;n++) {
  const version=n===20?'changed-version':'v1';
  const claim=await reserve(String(n),version);
  assert(claim.claim_token,'не выдана пилотная попытка '+n);
  await finish(String(n),claim.claim_token,false,version);
}
const pilotBlocked=await reserve('21','next-version');
assert(pilotBlocked.status==='limit_reached' && pilotBlocked.retry_after===null,'пилотный предел обошёлся сменой версии');
const live=await mode('live');
assert(live.attempts===20 && live.daily_attempts===20 && live.status==='available','live не учитывает пилот этого дня');
await mode('pilot');
assert((await reserve('21')).status==='limit_reached','смена режима сбросила lifetime');
await mode('live');
for(let n=21;n<=500;n++) {
  const claim=await reserve(String(n));
  assert(claim.claim_token,'не выдана разрешённая попытка '+n);
  await finish(String(n),claim.claim_token);
}
const limited=await reserve('501');
const atLimit=await state();
assert(limited.status==='limit_reached' && limited.retry_after===atLimit.reset_at,'не отклонена 501-я попытка');
assert(atLimit.attempts===500 && atLimit.daily_attempts===500,'ошибочный учёт попыток');
assert((await reserve('20','changed-version')).status==='ready','лимит закрыл готовый кэш');
assert((await reserve('20','another-version')).status==='limit_reached','смена версии повысила лимит');
await rpc("select public.unavailable_carousel_cover('999','v1') r");
assert((await state()).daily_attempts===500,'image_unavailable потратил попытку');
await mode('pilot'); await mode('live');
assert((await state()).attempts===500 && (await reserve('501')).status==='limit_reached','переключение вернуло бюджет');

// Проверяем полночь непосредственно в PostgreSQL и независимость от TimeZone сессии.
await db.exec("set time zone 'Pacific/Honolulu'");
for (const [at,start,reset] of [
  ['2026-10-23T20:59:59.999999Z','2026-10-22T21:00:00Z','2026-10-23T21:00:00Z'],
  ['2026-10-23T21:00:00Z','2026-10-23T21:00:00Z','2026-10-24T21:00:00Z'],
  ['2026-12-31T21:00:00Z','2026-12-31T21:00:00Z','2027-01-01T21:00:00Z']]) {
  const bounds=(await query('select start_at=$2::timestamptz as starts,reset_at=$3::timestamptz as resets from public.carousel_cover_day_window($1::timestamptz)',[at,start,reset])).rows[0];
  assert(bounds.starts && bounds.resets,'ошибка московской полуночи '+at);
}
assert((await state()).reset_at.endsWith('T21:00:00Z'),'reset_at зависит от TimeZone сессии');

// Только фикстура локального disposable PGlite меняет даты; production RPC часы не принимает.
await db.exec("reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=500,mode='live';");
await db.exec(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
  select n::text,'v1','no_text',gen_random_uuid(),w.start_at-interval '1 microsecond'
  from generate_series(1,500) n cross join public.carousel_cover_day_window(clock_timestamp()) w;`);
assert((await state()).daily_attempts===0 && (await state()).attempts===500,'предыдущий день не отделён от текущего');
await db.exec('set role service_role');
await mode('pilot');
assert((await reserve('501')).status==='limit_reached','полночь восстановила пилотный lifetime');
await mode('live');
const nextDay=await reserve('501');
assert(nextDay.claim_token,'новые сутки не восстановили дневной бюджет');
await finish('501',nextDay.claim_token);
assert((await state()).attempts===501 && (await state()).daily_attempts===1,'полночь сбросила lifetime или потеряла новую попытку');

// Полуинтервал [start,reset): ровно в начале считаем, ровно в конце не считаем.
await db.exec('reset role; truncate public.carousel_cover_translations;');
await db.exec(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
  select '1','v1','no_text',gen_random_uuid(),start_at from public.carousel_cover_day_window(clock_timestamp())
  union all select '2','v1','no_text',gen_random_uuid(),reset_at from public.carousel_cover_day_window(clock_timestamp())
  union all select '3','v1','image_unavailable',null,start_at from public.carousel_cover_day_window(clock_timestamp());`);
await db.exec('set role service_role');
assert((await state()).daily_attempts===1,'неверные включённые границы или учёт записи без claim');

// Два одновременно отправленных запроса претендуют на одну оставшуюся дневную попытку.
await db.exec("reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=499;");
await db.exec(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
  select n::text,'v1','no_text',gen_random_uuid(),clock_timestamp() from generate_series(1,499) n;`);
await db.exec('set role service_role');
const contenders=await Promise.all([reserve('500'),reserve('501')]);
assert(contenders.filter(r=>r.claim_token).length===1 && contenders.filter(r=>r.status==='limit_reached').length===1,'конкуренты получили 501-ю попытку');
assert((await state()).attempts===500 && (await state()).daily_attempts===500,'конкурентный бюджет вышел за предел');

// Зависшие слоты не освобождаются ни сменой режима, ни новой московской датой.
await db.exec("reset role; truncate public.carousel_cover_translations; update public.carousel_cover_budget set attempts=0,mode='pilot'; set role service_role;");
const unknown=await reserve('99');
await finish('99',unknown.claim_token,true);
await mode('live');
assert((await reserve('99')).status==='error','неопределённый результат запущен повторно');
const active=await reserve('100');
await db.exec("reset role; update public.carousel_cover_translations set created_at=created_at-interval '1 day'; set role service_role;");
assert(active.claim_token && (await reserve('101')).status==='busy','смена суток освободила неопределённый слот');
assert((await state()).attempts===2 && (await state()).daily_attempts===0,'busy потратил бюджет или смена дня сбросила lifetime');
await db.close();
console.log('SQL: права/RLS, claim, глобальные 2/20/500, смена режима, московская полночь и неизвестный исход проверены. PGlite выполняет запросы в одном runtime; несколько реальных соединений проверяются отдельно.');
''')
    result = subprocess.run(['node', str(script), module, str(migration)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
