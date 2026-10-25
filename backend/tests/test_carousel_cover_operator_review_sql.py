"""Операторское завершение оплаченной ошибки в локальном PostgreSQL без внешних API."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_carousel_cover_sql import PGLITE_PATHS

ROOT = Path(__file__).resolve().parents[2]


def test_claimed_error_operator_resolution_preserves_audit_and_budget(tmp_path):
    module = os.environ.get('CAROUSEL_COVER_TEST_PGLITE') or next(
        (path for path in PGLITE_PATHS if Path(path).is_file()), '')
    if not shutil.which('node') or not Path(module).is_file():
        pytest.skip('Локальный PGlite недоступен')
    script = tmp_path / 'operator-review.mjs'
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
for(const file of process.argv.slice(3))await db.exec(readFileSync(file,'utf8'));
await db.exec("set role service_role; update public.carousel_cover_budget set mode='live',attempts=8");
const row=id=>rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id=$1 and prompt_version='cover-v1'",[id]);
const state=()=>rpc('select public.carousel_cover_budget_state() r');
const hash='a'.repeat(64);
const resolve=(id,at,status='no_text',source='',imageHash=hash)=>rpc(
 'select public.resolve_carousel_cover_known($1,$2,$3,$4,$5) r',[id,at,status,source,imageHash]);
const rejects=async(id,at,status='no_text',source='',imageHash=hash)=>{
 try{await resolve(id,at,status,source,imageHash);return false}catch{return true}};
// Две оплаченные попытки одного ID и событие первого восстановления уже существуют.
await q(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,model,
 image_hash,usage,duration_ms,created_at,finished_at,lease_expires_at,reason_code,source_kind)
 values('1','cover-v1','error',gen_random_uuid(),'unchanged-model',$1,'{"error_type":"ValidationError"}',123,
 clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '600 seconds','provider_outcome_unknown','media_url'),
 ('1','cover-recovery-audit-old','error',gen_random_uuid(),'old-model',$1,'{"total_token_count":20}',100,
 clock_timestamp()-interval '1 hour',clock_timestamp()-interval '1 hour',clock_timestamp()-interval '50 minutes','provider_error','public_first_cover')`,[hash]);
await q("insert into public.carousel_cover_recovery_events(media_id,original_created_at,reason) values('1',clock_timestamp()-interval '1 hour','paid_retry_after_confirmed_expiry')");
const eventBefore=await rpc("select to_jsonb(e) r from public.carousel_cover_recovery_events e where media_id='1'");
const oldAudit=await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='1' and prompt_version='cover-recovery-audit-old'");
let first=await row('1');
assert(await rejects('1',first.created_at),'свежий неизвестный запрос принят');
assert(await rejects('1','2000-01-01T00:00Z'),'CAS пропустил старую дату');
await q("update public.carousel_cover_translations set lease_expires_at=clock_timestamp()-interval '1 second' where media_id='1' and prompt_version='cover-v1'");
assert(await rejects('1',first.created_at),'lease обошёл минимальные600секунд');
await q("update public.carousel_cover_translations set created_at=clock_timestamp()-interval '601 seconds',lease_expires_at=clock_timestamp()+interval '1 second' where media_id='1' and prompt_version='cover-v1'");
first=await row('1');
assert(await rejects('1',first.created_at),'живой lease принят после600секунд');
await q("update public.carousel_cover_translations set lease_expires_at=clock_timestamp()-interval '1 second' where media_id='1' and prompt_version='cover-v1'");
first=await row('1');
assert(await rejects('1',first.created_at,'no_text','','b'.repeat(64)),'чужой hash принят');
for(const status of ['ready','unreadable','error','image_unavailable','running'])
 assert(await rejects('1',first.created_at,status),'запрещённый результат '+status);
const before=await state();
const result=await resolve('1',first.created_at);
assert(result.status==='no_text'&&!result.attempt_recorded&&result.reason_code==='operator_visual_review','неверный результат');
const terminal=await row('1');
assert(terminal.claim_token===null&&terminal.model===null&&!terminal.slot_held,'ручной результат получил claim/model');
assert(terminal.image_hash===hash&&terminal.usage.provenance==='operator_visual_review','потерян исходник/provenance');
assert(terminal.reason_code==='operator_visual_review','потеряна причина');
const audit=await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='1' and prompt_version=$1",[terminal.usage.previous_audit_version]);
assert(JSON.stringify({...audit,prompt_version:'cover-v1'})===JSON.stringify(first),'оплаченная ошибка изменена при архивации');
assert(JSON.stringify(await rpc("select to_jsonb(t) r from public.carousel_cover_translations t where media_id='1' and prompt_version='cover-recovery-audit-old'"))===JSON.stringify(oldAudit),'старый audit изменён');
assert(JSON.stringify(await rpc("select to_jsonb(e) r from public.carousel_cover_recovery_events e where media_id='1'"))===JSON.stringify(eventBefore),'recovery event перезаписан');
assert(JSON.stringify(await state())===JSON.stringify(before),'ручной результат изменил расход/слоты');
assert((await q("select count(*)::int n from public.carousel_cover_translations where media_id='1' and claim_token is not null")).rows[0].n===2,'оплаченная попытка исчезла');
assert(await rejects('1',terminal.created_at),'повторный ручной resolve принят');
assert(await fail("select public.rearm_carousel_cover('1',$1,'third paid retry')",[terminal.created_at]),'terminal rearm принят');
assert((await rpc("select public.reserve_carousel_cover('1','cover-v1','m',$1) r",[hash])).status==='no_text','terminal создал платную попытку');
// Явный завершённый ответ разрешает операторское завершение доTTL.
await q(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,model,image_hash,usage,lease_expires_at)
 values('2','cover-v1','error',gen_random_uuid(),'m',$1,'{"provider_response_complete":true,"total_token_count":17}',clock_timestamp()+interval '600 seconds'),
 ('3','cover-v1','error',gen_random_uuid(),'m',$1,'{"provider_response_complete":"true"}',clock_timestamp()+interval '600 seconds')`,[hash]);
const second=await row('2');
assert(await rejects('2',second.created_at,'no_text','','b'.repeat(64)),'complete обошёл hash');
const secondBefore=await state();
await resolve('2',second.created_at,'not_needed','Точный русский исходник');
assert(JSON.stringify(await state())===JSON.stringify(secondBefore),'complete refund/новая попытка');
assert((await q("select count(*)::int n from public.carousel_cover_recovery_events where media_id='2'")).rows[0].n===1,'событие отсутствует/дублировано');
assert((await row('2')).source_text==='Точный русский исходник'&&(await row('2')).text_ru==='','ручной исходник изменён');
assert(await rejects('3',(await row('3')).created_at),'строка true принята за доказательство ответа');
// Удерживаемый slot, ошибка без claim и terminal-строки закрыты даже с complete=true.
for(const [id,status,claimed,held] of [['4','error',true,true],['5','error',false,false],
 ['6','ready',true,false],['7','not_needed',true,false],['8','no_text',true,false]]){
 await q(`insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,slot_held,source_text,text_ru,image_hash,usage)
 values($1,'cover-v1',$2,case when $3 then gen_random_uuid() else null end,$4,'Исходник',case when $2='ready' then 'Перевод' else '' end,$5,'{"provider_response_complete":true}')`,[id,status,claimed,held,hash]);
 assert(await rejects(id,(await row(id)).created_at),'закрытое состояние принято '+id);
}
// Старая бесплатная ветка продолжает работать, повтор/recovery-event запрещены.
await rpc("select public.unavailable_carousel_cover('9','cover-v1') r");
await resolve('9',(await row('9')).created_at);
await rpc("select public.unavailable_carousel_cover('10','cover-v1') r");
await q("insert into public.carousel_cover_recovery_events(media_id,original_created_at,reason) values('10',clock_timestamp(),'already_used')");
assert(await rejects('10',(await row('10')).created_at),'старая одноразовая защита потеряна');
await db.exec('reset role');
for(const role of ['anon','authenticated']){
 await db.exec('set role '+role);
 assert(await fail("select public.resolve_carousel_cover_known('1',now(),'no_text','',repeat('a',64))"),'RPC открыт '+role);
 await db.exec('reset role');
}
assert((await q("select prosecdef from pg_proc where pronamespace='public'::regnamespace and proname='resolve_carousel_cover_known'")).rows[0].prosecdef===false,'RPC получил security definer');
await db.close();
console.log('Проверены CAS/hash/600s/complete/slots, обе ветки, неизменяемый audit/event, расход, terminal и ACL');
''')
    migrations = [ROOT / 'supabase/migrations' / name for name in (
        '20261020123535_carousel_cover_translations.sql',
        '20261023095816_carousel_cover_today_budget.sql',
        '20261024093435_carousel_cover_recovery.sql',
        '20261025073556_carousel_cover_completed_response_review.sql',
    )]
    result = subprocess.run(['node', str(script), module, *map(str, migrations)],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
