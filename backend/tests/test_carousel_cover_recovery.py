"""Исполняемая проверка восстановления без модели и удалённой БД."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_carousel_cover_sql import PGLITE_PATHS

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'supabase/migrations/20261024093435_carousel_cover_recovery.sql'


def test_lease_and_rearm_contract_in_postgres(tmp_path):
    module = os.environ.get('CAROUSEL_COVER_TEST_PGLITE') or next(
        (path for path in PGLITE_PATHS if Path(path).is_file()), '')
    if not shutil.which('node') or not Path(module).is_file():
        pytest.skip('Локальный PGlite недоступен')
    script = tmp_path / 'recovery.mjs'
    script.write_text(r'''
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {PGlite}=await import(pathToFileURL(process.argv[2]));
const db=new PGlite();
const assert=(v,m)=>{if(!v)throw Error(m)};
const q=(s,a=[])=>db.query(s,a);
const rpc=async(s,a=[])=> (await q(s,a)).rows[0].r;
const fail=async(s,a=[])=>{try{await q(s,a);return false}catch{return true}};
await db.exec('create role anon; create role authenticated; create role service_role bypassrls');
for(const file of process.argv.slice(3,5))await db.exec(readFileSync(file,'utf8'));
// Старое приложение уже держит claim и сохраняет кэш до ADD-миграции.
await db.exec(`insert into public.carousel_cover_translations(media_id,prompt_version,status,source_text,text_ru)
  values('10','cover-v1','ready','Русский заголовок','Русский заголовок');`);
const legacy=await rpc("select public.reserve_carousel_cover('11','cover-v1','m',repeat('a',64)) r");
const snapshot=await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='10'");
const ddl=readFileSync(process.argv[5],'utf8');
await db.exec(ddl);
assert((await q("select lease_expires_at=created_at+interval '600 seconds' ok from public.carousel_cover_translations where media_id='11'")).rows[0].ok,'legacy lease не600');
const after=await rpc("select to_jsonb(t)-'lease_expires_at'-'reason_code'-'source_kind' r from public.carousel_cover_translations t where media_id='10'");
assert(JSON.stringify(snapshot)===JSON.stringify(after),'DDL изменил готовый cache');
// Старые exact-сигнатуры finish/unavailable/reserve работают после DDL.
await rpc("select public.finish_carousel_cover('11','cover-v1',$1,'ready','Title','Заголовок','{}',1,false) r",[legacy.claim_token]);
await rpc("select public.unavailable_carousel_cover('12','cover-v1') r");
const reserve=id=>rpc("select public.reserve_carousel_cover($1,'cover-v1','m',repeat('a',64)) r",[id]);
const state=()=>rpc('select public.carousel_cover_budget_state() r');
const rearm=(id,at)=>rpc('select public.rearm_carousel_cover($1,$2,$3) r',[id,at,'Подтверждённый manifest']);
const created=async id=>(await q("select created_at::text at from public.carousel_cover_translations where media_id=$1 and prompt_version='cover-v1'",[id])).rows[0].at;
assert(await fail("select public.rearm_carousel_cover('10',$1,'test')",[await created('10')]),'ready разрешён для rearm');
const before12=await rpc("select to_jsonb(t)-'prompt_version' r from public.carousel_cover_translations t where media_id='12'");
await rearm('12',await created('12'));
assert(JSON.stringify(before12)===JSON.stringify(await rpc("select to_jsonb(t)-'prompt_version' r from public.carousel_cover_translations t where media_id='12'")),'audit изменил исходную строку');
assert((await q("select count(*)::int n from public.carousel_cover_translations where media_id='12' and prompt_version like 'cover-recovery-audit-%'")).rows[0].n===1,'audit потерян');
await rpc("select public.unavailable_carousel_cover('12','cover-v1') r");
assert(await fail("select public.rearm_carousel_cover('12',$1,'test')",[await created('12')]),'разрешён второй recovery');
await rpc("select public.unavailable_carousel_cover('13','cover-v1') r");
assert(await fail("select public.rearm_carousel_cover('13','2000-01-01','race')"),'CAS обойдён');
const at13=await created('13');
const races=await Promise.allSettled([rearm('13',at13),rearm('13',at13)]);
assert(races.filter(r=>r.status==='fulfilled').length===1,'два rearm обошли CAS');
const a=await reserve('20'), b=await reserve('21');
assert(a.claim_token&&b.claim_token&&(await reserve('22')).status==='busy','два слота нарушены');
assert((await q("select lease_expires_at=created_at+interval '600 seconds' ok from public.carousel_cover_translations where media_id='20' and prompt_version='cover-v1'")).rows[0].ok,'новый lease не600');
const before=await state();
assert(before.active_slots===2&&before.available_slots===0,'нет счётчиков slots');
assert((await reserve('20')).claim_token===undefined,'cache выдал claim повторно');
await db.exec("update public.carousel_cover_translations set lease_expires_at=clock_timestamp()-interval '1 second' where media_id='20'");
assert(await fail("select public.finish_carousel_cover('20','cover-v1',$1,'ready','Late','Поздно','{}',1,false)",[a.claim_token]),'late finish принят');
await rpc('select public.reap_carousel_cover_leases() r');
const expired=await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='20'");
assert(expired.status==='error'&&expired.reason_code==='lease_expired_unknown'&&!expired.slot_held,'lease не освобождён');
assert((await state()).attempts===before.attempts&&(await state()).daily_attempts===before.daily_attempts,'reap вернул бюджет');
assert(await fail("select public.finish_carousel_cover('20','cover-v1',$1,'ready','Late','Поздно','{}',1,false)",[a.claim_token]),'reaped finish принят');
const oldClaim=expired.claim_token;
await rearm('20',expired.created_at);
const retry=await reserve('20');
assert(retry.claim_token&&retry.claim_token!==oldClaim,'новый claim не fenced');
assert(await fail("select public.finish_carousel_cover('20','cover-v1',$1,'ready','Late','Поздно','{}',1,false)",[oldClaim]),'старый token принят после rearm');
await rpc("select public.finish_carousel_cover('20','cover-v1',$1,'not_needed','Русский текст','','{}',1,false) r",[retry.claim_token]);
assert((await reserve('20')).status==='not_needed','not_needed cache потерян');
await rpc("select public.finish_carousel_cover('21','cover-v1',$1,'error',null,null,'{}',1,true) r",[b.claim_token]);
assert((await state()).active_slots===1,'uncertain потерял слот до срока');
await db.exec("update public.carousel_cover_translations set lease_expires_at=clock_timestamp()-interval '1 second' where media_id='21'");
const next=await reserve('22');
assert(next.claim_token&&(await state()).active_slots===1,'reserve не reaped uncertain');
assert((await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='21'")).reason_code==='lease_expired_unknown','uncertain потерял причину');
const limited429=await rpc("select public.finish_carousel_cover('22','cover-v1',$1,'error',null,null,$2,1,false) r",
 [next.claim_token,{provider_status:429,provider_response_complete:true,retry_after:'2030-01-01T00:00:00Z'}]);
assert(limited429.reason_code==='provider_rate_limit'&&limited429.retry_after==='2030-01-01T00:00:00Z'&&limited429.attempt_recorded,'429 не вернул safe pause');
assert((await state()).active_slots===0,'завершённый429 удержал слот');
assert((await reserve('22')).retry_after===limited429.retry_after,'cache429 потерял срокpause');
const imageFailure=await rpc("select public.unavailable_carousel_cover_source('40','cover-v1','timeout','public_first_cover') r");
assert(imageFailure.reason_code==='timeout'&&!imageFailure.attempt_recorded,'imagefailure потерял metadata');
// Проверенный оператором исходник завершается бесплатно, исходная строка остаётся в audit.
await rpc("select public.unavailable_carousel_cover('30','cover-v1') r");
const countBefore=await state();
const at30=await created('30');
const resolved=await rpc("select public.resolve_carousel_cover_known('30',$1,'not_needed','Точный русский текст',repeat('b',64)) r",[at30]);
assert(resolved.status==='not_needed'&&!resolved.attempt_recorded,'операторский результат не terminal');
const row30=await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='30' and prompt_version='cover-v1'");
assert(row30.claim_token===null&&row30.model===null&&row30.usage.provenance==='operator_visual_review','операторский результат получил модельный claim');
assert((await state()).attempts===countBefore.attempts,'операторский resolve потратил попытку');
assert(await fail("select public.resolve_carousel_cover_known('30',$1,'no_text','',repeat('b',64))",[row30.created_at]),'terminal перезаписан');
for(const [at,limit] of [['2026-10-22T20:59:59Z',500],['2026-10-22T21:00:00Z',2500],['2026-10-23T21:00:00Z',2500],['2026-10-24T20:59:59.999999Z',2500],['2026-10-24T21:00:00Z',500]])
 assert(await rpc('select public.carousel_cover_daily_limit($1) r',[at])===limit,'ошибка MSK '+at);
for(const role of ['anon','authenticated']){
 await db.exec('set role '+role);
 for(const s of ['select public.reap_carousel_cover_leases()',"select public.rearm_carousel_cover('12',now(),'test')",'select * from public.carousel_cover_recovery_events',"select public.resolve_carousel_cover_known('30',now(),'no_text','',repeat('b',64))"])
  assert(await fail(s),role+' получил recovery RPC/table');
 await db.exec('reset role');
}
assert(ddl.includes("v_now+interval '600 seconds'"),'неверный lease');
// Управляемые часы существуют только в disposable fixture, production API не принимает время.
await db.exec(`create function public.cover_recovery_test_now() returns timestamptz language sql volatile as $$
 select current_setting('cover_recovery.at')::timestamptz $$`);
for(const name of ['carousel_cover_budget_state','reserve_carousel_cover','finish_carousel_cover','reap_carousel_cover_leases']){
 const definition=(await q("select pg_get_functiondef(oid) definition from pg_proc where pronamespace='public'::regnamespace and proname=$1",[name])).rows[0].definition;
 await db.exec(definition.replaceAll('clock_timestamp()','public.cover_recovery_test_now()'));
}
await db.exec(`truncate public.carousel_cover_translations;update public.carousel_cover_budget set mode='live',attempts=2700;
 select set_config('cover_recovery.at','2026-10-24T20:59:59Z',false);
 insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
 select n::text,'cover-v1','no_text',gen_random_uuid(),'2026-10-24T00:00Z' from generate_series(1,2499) n;`);
const last=await reserve('2500');
assert(last.claim_token&&(await reserve('2501')).status==='limit_reached','2500/2501 нарушен');
assert((await state()).attempts===2701&&(await state()).daily_attempts===2500,'неверный учёт2500');
await rpc("select public.finish_carousel_cover('2500','cover-v1',$1,'no_text','','','{}',1,false) r",[last.claim_token]);
await db.exec(`select set_config('cover_recovery.at','2026-10-24T21:00:00Z',false)`);
const midnight=await reserve('2501');
assert(midnight.claim_token&&(await state()).daily_limit===500&&(await state()).daily_attempts===1,'полночь не вернула500');
assert((await state()).attempts===2702,'полночь сбросила lifetime');
await db.exec(`truncate public.carousel_cover_translations;update public.carousel_cover_budget set attempts=3200;
 insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,created_at)
 select n::text,'cover-v1','no_text',gen_random_uuid(),'2026-10-24T21:00:00Z' from generate_series(1,499) n;`);
assert((await reserve('500')).claim_token&&(await reserve('501')).status==='limit_reached','500/501 после повышения нарушен');

await db.close();
console.log('Проверены old-app ADD compatibility, lease/fencing, slots/counters, audit/CAS/once, not_needed, MSK и роли');
''')
    result = subprocess.run(['node', str(script), module,
        str(ROOT / 'supabase/migrations/20261020123535_carousel_cover_translations.sql'),
        str(ROOT / 'supabase/migrations/20261023095816_carousel_cover_today_budget.sql'), str(MIGRATION)],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
