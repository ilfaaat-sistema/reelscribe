"""Независимые проверки контракта обложек.

Сеть, Vertex и удалённая Supabase не используются. SQL исполняется в PGlite.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from app.api import carousel_cover_translations as cover_api
from app.services import carousel_cover_auth as cover_auth
from app.services import carousel_cover_image as cover_image
from app.services import carousel_cover_service as cover_service
from app.services import carousel_cover_vertex as cover_vertex
from app.services.carousel_cover_settings import CoverSettings


PGLITE_PATHS = (
    '/private/tmp/reels-authors-sql-test/node_modules/@electric-sql/pglite/dist/index.js',
    '/private/tmp/residents-export-sql/node_modules/@electric-sql/pglite/dist/index.js',
    '/private/tmp/resident-report-dbqa/node_modules/@electric-sql/pglite/dist/index.js',
)


def test_http_boundary_accepts_ids_and_period_only(monkeypatch):
    translate = AsyncMock(return_value={'item': {'id': '1', 'status': 'disabled',
                                                  'source_text': None, 'text_ru': None}})
    read = AsyncMock(return_value={'items': [], 'enabled': False})
    monkeypatch.setattr(cover_api.service, 'translate', translate)
    monkeypatch.setattr(cover_api.service, 'read', read)
    app = FastAPI()
    app.include_router(cover_api.router, prefix='/api')
    client = TestClient(app)
    base = '/api/radar/carousel-cover-translations'

    response = client.get(base, params={'ids': '1,1,2'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    read.assert_awaited_once_with(['1', '2'])
    assert client.get(base, params={'ids': ','.join(map(str, range(101)))}).status_code == 422
    assert client.get(base, params={'ids': 'https://x.fbcdn.net/private'}).status_code == 422
    assert client.post(base + '/1', json={'period': '7d', 'url': 'https://x.fbcdn.net/a'}).status_code == 422
    assert client.post(base + '/1', json={'period': 'tomorrow'}).status_code == 422
    assert client.post(base + '/not-an-id', json={'period': '7d'}).status_code == 422
    translate.assert_not_awaited()
    response = client.post(base + '/1', json={'period': 'archive14d'},
                           headers={'x-vercel-oidc-token': 'request-token'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    translate.assert_awaited_once_with('1', 'archive14d', oidc_token='request-token')


def test_downloader_rechecks_redirect_before_requesting_target():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://127.0.0.1/credential'})

    async def run():
        with pytest.raises(cover_image.ImageUnavailable):
            await cover_image.download_image(
                'https://scontent.cdninstagram.com/cover',
                client_factory=lambda **kwargs: httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), **kwargs),
            )

    asyncio.run(run())
    assert seen == ['https://scontent.cdninstagram.com/cover']


class AtomicStore:
    """Модель публичного store-контракта; SQL-атомарность отдельно доказывает PGlite."""

    def __init__(self):
        from threading import Lock
        self.lock = Lock()
        self.rows = {}
        self.attempts = 0
        self.held = set()

    def budget(self):
        limited = self.attempts >= 20
        return {'status': 'limit_reached' if limited else 'available', 'mode': 'pilot',
                'pilot_limit': 20, 'daily_limit': 500,
                'reset_at': '2030-01-01T21:00:00Z', 'retry_after': None,
                'attempts': self.attempts, 'daily_attempts': self.attempts}

    def read(self, ids):
        with self.lock:
            return [dict(self.rows[identity], media_id=identity)
                    for identity in ids if identity in self.rows]

    def reserve(self, media_id, _model, _image_hash):
        with self.lock:
            if media_id in self.rows:
                return dict(self.rows[media_id])
            if self.attempts >= 20:
                return {'status': 'limit_reached'}
            if len(self.held) >= 2:
                return {'status': 'busy'}
            self.attempts += 1
            self.held.add(media_id)
            self.rows[media_id] = {'status': 'running'}
            return {'status': 'running', 'claim_token': 'claim-' + media_id}

    def finish(self, media_id, token, result, _usage, _duration, uncertain=False):
        assert token == 'claim-' + media_id
        with self.lock:
            self.rows[media_id] = dict(result)
            if not uncertain:
                self.held.remove(media_id)
            return dict(result)

    def unavailable(self, media_id):
        with self.lock:
            return self.rows.setdefault(media_id, {'status': 'image_unavailable'})


def service_fixture(monkeypatch):
    store = AtomicStore()
    monkeypatch.setattr(cover_service, 'CoverStore', lambda: store)
    monkeypatch.setattr(cover_service, 'get_settings',
                        lambda: SimpleNamespace(enabled=True, vertex_model='vertex-model'))
    monkeypatch.setattr(cover_service.carousels, 'detail',
                        AsyncMock(return_value={'media_url': 'https://x.fbcdn.net/a'}))
    monkeypatch.setattr(cover_service, 'download_image',
                        AsyncMock(return_value=(b'\xff\xd8\xffimage', 'image/jpeg')))
    client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock())
    monkeypatch.setattr(cover_service, 'create_vertex_client', Mock(return_value=client))
    return store


def test_two_tabs_share_one_paid_claim_and_timeout_keeps_global_slot(monkeypatch):
    store = service_fixture(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def model(*_args):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {'status': 'ready', 'source_text': 'Title', 'text_ru': 'Заголовок'}, {}

    monkeypatch.setattr(cover_service, 'generate', model)

    async def two_tabs():
        owner = asyncio.create_task(cover_service.translate('10', '7d'))
        await entered.wait()
        follower = await cover_service.translate('10', '30d')
        assert follower['item']['status'] == 'running'
        release.set()
        ready = await owner
        assert ready['item']['status'] == 'ready'

    asyncio.run(two_tabs())
    assert calls == store.attempts == 1

    async def fail_model(*_args):
        raise TimeoutError('provider raw credential')

    monkeypatch.setattr(cover_service, 'generate', fail_model)

    async def uncertain_slots():
        first = await cover_service.translate('20', '7d')
        second = await cover_service.translate('21', '7d')
        third = await cover_service.translate('22', '7d')
        again = await cover_service.translate('20', 'archive14d')
        return first, second, third, again

    first, second, third, again = asyncio.run(uncertain_slots())
    assert [value['item']['status'] for value in (first, second, third, again)] == [
        'error', 'error', 'busy', 'error']
    assert store.held == {'20', '21'}
    assert store.attempts == 3  # 10 завершён; 20/21 удерживают два слота.


def test_vertex_client_forces_adc_vertex_and_one_total_attempt(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv('GEMINI_API_KEY', 'must-not-be-forwarded')
    monkeypatch.setattr(cover_vertex, 'get_settings', lambda: SimpleNamespace(
        enabled=True, vertex_project='project-id', vertex_location='global', auth_mode='adc'))
    from google import genai
    monkeypatch.setattr(genai, 'Client', fake_client)
    cover_vertex.create_vertex_client()

    assert captured['vertexai'] is True
    assert captured['project'] == 'project-id'
    assert captured['location'] == 'global'
    assert 'api_key' not in captured
    assert captured['credentials'] is None
    assert captured['http_options'].timeout == cover_vertex.MODEL_TIMEOUT_MS
    assert captured['http_options'].retry_options.attempts == 1


def test_gemini_flash_payload_uses_low_thinking_without_temperature(monkeypatch):
    captured = {}

    async def generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            parsed={'status': 'ready', 'source_text': 'Title', 'text_ru': 'Заголовок'},
            text='', usage_metadata=None,
        )

    client = SimpleNamespace(aio=SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(cover_vertex, 'get_settings',
                        lambda: SimpleNamespace(vertex_model='gemini-3.8-flash'))
    result, usage = asyncio.run(cover_vertex.generate(client, b'image', 'image/jpeg'))
    payload = captured['config'].model_dump(by_alias=True, exclude_none=True)

    assert captured['model'] == 'gemini-3.8-flash'
    assert 'temperature' not in payload
    assert payload['thinkingConfig']['thinkingLevel'] == 'LOW'
    assert payload['responseMimeType'] == 'application/json'
    assert result == {'status': 'ready', 'source_text': 'Title', 'text_ru': 'Заголовок'}
    assert usage == {}


def oidc_config():
    return CoverSettings(
        vertex_project='test-project', auth_mode='vercel_oidc',
        wif_audience=('//iam.googleapis.com/projects/123/locations/global/'
                      'workloadIdentityPools/test/providers/vercel'),
        service_account_email='cover@test-project.iam.gserviceaccount.com',
        oidc_issuer='https://oidc.vercel.com/test-team',
        oidc_audience='https://vercel.com/test-team', oidc_owner_id='team_123',
        oidc_project_id='prj_123', oidc_team='test-team', oidc_project_name='reelscribe',
    )


def oidc_token(**changes):
    now = int(time.time())
    claims = {
        'iss': oidc_config().oidc_issuer, 'aud': oidc_config().oidc_audience,
        'owner_id': 'team_123', 'project_id': 'prj_123', 'owner': 'test-team',
        'project': 'reelscribe', 'environment': 'preview',
        'sub': 'owner:test-team:project:reelscribe:environment:preview',
        'iat': now - 1, 'nbf': now - 1, 'exp': now + 60,
    }
    claims.update(changes)

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')

    return encode({'alg': 'RS256', 'kid': 'test-key'}) + '.' + encode(claims) + '.signature'


@pytest.mark.parametrize('claims', [
    {'iss': 'https://attacker.invalid'}, {'aud': 'wrong-audience'}, {'sub': 'wrong-subject'},
    {'owner_id': 'other-owner'}, {'project_id': 'other-project'}, {'exp': 1},
])
def test_oidc_wrong_identity_and_expiry_fail_closed_without_token(claims):
    token = oidc_token(**claims)
    with pytest.raises(cover_auth.CoverAuthError) as caught:
        cover_auth.validate_claims(token, oidc_config())
    assert str(caught.value) == 'Серверная авторизация обложек недоступна'
    assert token not in str(caught.value)


def test_oidc_refresh_failure_is_safe_and_supplier_rechecks_expiry(monkeypatch):
    token = oidc_token()
    supplier = cover_auth.RequestTokenSupplier(token, oidc_config())
    assert supplier.get_subject_token(None, None) == token
    real_time = time.time
    monkeypatch.setattr(cover_auth.time, 'time', lambda: 9_999_999_999)
    with pytest.raises(RefreshError) as expired:
        supplier.get_subject_token(None, None)
    assert token not in str(expired.value)

    constructed = []

    class Credentials:
        def __init__(self, **kwargs):
            assert kwargs['subject_token_supplier']._token == token
            constructed.append(True)

        def refresh(self, _request):
            raise RuntimeError('private-provider-response')

    monkeypatch.setattr(cover_auth.time, 'time', real_time)
    monkeypatch.setattr(cover_auth.identity_pool, 'Credentials', Credentials)
    with pytest.raises(cover_auth.CoverAuthError) as caught:
        cover_auth.oidc_credentials(token, config=oidc_config())
    assert constructed == [True]
    assert 'private-provider-response' not in str(caught.value)
    assert token not in str(caught.value)


def test_pglite_pilot_live_moscow_midnight_slots_and_rls(tmp_path):
    pglite = next((Path(value) for value in PGLITE_PATHS if Path(value).is_file()), None)
    if pglite is None:
        pytest.skip('PGlite недоступен')
    repo = Path(cover_service.__file__).resolve().parents[3]
    migration = repo / 'supabase/migrations/20261020123535_carousel_cover_translations.sql'
    assert migration.is_file(), migration
    script = tmp_path / 'acceptance.mjs'
    script.write_text(r'''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(pathToFileURL(process.argv[2]));
const db = new PGlite();
const ok = (value, message) => { if (!value) throw new Error(message); };
await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
await db.exec(readFileSync(process.argv[3], 'utf8'));
const q = (sql, args=[]) => db.query(sql, args);
const reserve = async (id, version='v1') => (await q(
  'select public.reserve_carousel_cover($1,$2,$3,$4) value',
  [id, version, 'model', 'a'.repeat(64)])).rows[0].value;
const finish = async (id, version, token, uncertain=false) => (await q(
  'select public.finish_carousel_cover($1,$2,$3,$4,$5,$6,$7,$8,$9) value',
  [id, version, token, uncertain ? 'error' : 'ready', 'Title', 'Заголовок', {}, 1, uncertain])).rows[0].value;

for (const role of ['anon', 'authenticated']) {
  await db.exec('set role ' + role);
  for (const sql of [
    'select * from public.carousel_cover_budget',
    'select * from public.carousel_cover_translations',
    "select public.reserve_carousel_cover('1','v1','m',repeat('a',64))",
    "select public.unavailable_carousel_cover('1','v1')",
    "select public.finish_carousel_cover('1','v1','00000000-0000-0000-0000-000000000000','error',null,null,'{}',1,false)",
    'select public.carousel_cover_budget_state()',
    "select * from public.carousel_cover_day_window(clock_timestamp())",
    "select public.set_carousel_cover_mode('live')",
  ]) {
    let denied = false;
    try { await q(sql); } catch (error) { denied = error.message.includes('permission denied'); }
    ok(denied, role + ' unexpectedly allowed: ' + sql);
  }
  await db.exec('reset role');
}

await db.exec('set role service_role');
for (let n = 1; n <= 20; n++) {
  const version = n === 20 ? 'changed-prompt-and-model' : 'v1';
  const claim = await reserve(String(n), version);
  ok(claim.claim_token, 'attempt ' + n + ' was not reserved');
  await finish(String(n), version, claim.claim_token, n === 20);
}
const pilotLimit = await reserve('900', 'another-version');
ok(pilotLimit.status === 'limit_reached' && pilotLimit.retry_after === null, 'pilot limit/version reset');
let budget = (await q('select public.carousel_cover_budget_state() value')).rows[0].value;
ok(budget.mode === 'pilot' && budget.attempts === 20 && budget.daily_attempts === 20, 'pilot counters');

budget = (await q("select public.set_carousel_cover_mode('live') value")).rows[0].value;
ok(budget.mode === 'live' && budget.attempts === 20 && budget.daily_attempts === 20, 'switch reset counters');
const secondSlot = await reserve('21');
ok(secondSlot.claim_token && (await reserve('22')).status === 'busy', 'unknown slot was released');
await finish('21', 'v1', secondSlot.claim_token);
const beforeFree = (await q('select public.carousel_cover_budget_state() value')).rows[0].value;
await q("select public.unavailable_carousel_cover('700','v1')");
const afterFree = (await q('select public.carousel_cover_budget_state() value')).rows[0].value;
ok(beforeFree.attempts === afterFree.attempts && beforeFree.daily_attempts === afterFree.daily_attempts,
  'image_unavailable spent budget');

await db.exec('reset role');
await db.exec(`insert into public.carousel_cover_translations
  (media_id,prompt_version,status,source_text,text_ru,claim_token,slot_held,model,image_hash,finished_at)
  select n::text,'v1','ready','Title','Заголовок',gen_random_uuid(),false,'model',repeat('b',64),clock_timestamp()
  from generate_series(22,499) n;
  update public.carousel_cover_budget set attempts=attempts+478 where singleton;`);
await db.exec('set role service_role');
const fiveHundred = await reserve('500');
ok(fiveHundred.claim_token, '500th daily attempt denied');
await finish('500', 'v1', fiveHundred.claim_token);
const over = await reserve('501');
ok(over.status === 'limit_reached' && typeof over.retry_after === 'string', '501st daily attempt allowed');
budget = (await q('select public.carousel_cover_budget_state() value')).rows[0].value;
const reset = (await q(`select to_char(reset_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') value
  from public.carousel_cover_day_window(clock_timestamp())`)).rows[0].value;
ok(budget.attempts === 500 && budget.daily_attempts === 500, 'live counters are not 500');
ok(budget.retry_after === reset && budget.reset_at === reset, 'reset is not Moscow midnight in UTC');

await db.exec('reset role');
await db.exec(`update public.carousel_cover_translations set created_at =
  (select start_at - interval '1 second' from public.carousel_cover_day_window(clock_timestamp()))
  where claim_token is not null;`);
await db.exec('set role service_role');
budget = (await q('select public.carousel_cover_budget_state() value')).rows[0].value;
ok(budget.attempts === 500 && budget.daily_attempts === 0 && budget.status === 'available', 'midnight reset lifetime');
const nextDay = await reserve('501');
ok(nextDay.claim_token, 'new Moscow day did not reopen budget');
ok((await reserve('502')).status === 'busy', 'midnight released unknown slot');
ok((await q('select attempts from public.carousel_cover_budget')).rows[0].attempts === 501, 'lifetime was reset');
await db.close();
''', encoding='utf-8')
    result = subprocess.run(
        ['node', str(script), str(pglite), str(migration)],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, 'NO_COLOR': '1'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
