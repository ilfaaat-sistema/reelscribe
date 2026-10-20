"""Контракт HTTP и жизненный цикл перевода; SQL прогоняется отдельно в изоляции."""
from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import carousel_translation, carousels
from app.core import translation_db
from app.pipeline.translate import TranslationFailedError, TranslationQuotaExceededError
from app.services import carousel_translation as service

SOURCE = 'This is an English caption which should be translated to Russian for the reader.'
RUSSIAN = 'Это английская подпись, которую нужно перевести на русский язык для читателя.'
URL = '/api/radar/carousels/123'


class Query:
    def __init__(self, db):
        self.db, self.filters, self.deleting = db, {}, False

    def select(self, columns):
        assert columns == service.COLUMNS
        return self

    def eq(self, name, value):
        self.filters[name] = value
        return self

    def limit(self, value):
        assert value == 1
        return self

    def delete(self):
        self.deleting = True
        return self

    def execute(self):
        if self.db.fail:
            raise RuntimeError('закрытый ответ БД')
        key = self.filters['instagram_media_id'], self.filters['source_hash']
        row = self.db.cache.get(key)
        if self.deleting and row and row['translation'] == self.filters['translation']:
            del self.db.cache[key]
        return SimpleNamespace(data=[row] if row else [])


class DB:
    def __init__(self):
        self.cache, self.attempts, self.calls = {}, {}, []
        self.fail, self.fail_finish, self.stale, self.reserve_state = False, False, False, None

    def table(self, name):
        assert name == service.TABLE
        return Query(self)

    def rpc(self, name, args):
        self.calls.append((name, args))

        def execute():
            if self.fail or (self.fail_finish and name.startswith('finish')):
                raise RuntimeError('закрытый ответ БД')
            key = args['p_id'], args['p_source_hash']
            token = args['p_attempt_id']
            if name == 'reserve_carousel_caption_translation':
                if self.reserve_state:
                    return SimpleNamespace(data={'state': self.reserve_state, 'retry_after': 27})
                if key in self.cache:
                    return SimpleNamespace(data={'state': 'ready', **self.cache[key]})
                if any(row['key'] == key and not row.get('outcome') for row in self.attempts.values()):
                    return SimpleNamespace(data={'state': 'in_progress'})
                self.attempts[token] = {'key': key}
                return SimpleNamespace(data={'state': 'reserved', 'attempt_id': token})
            assert name == 'finish_carousel_caption_translation'
            if self.stale:
                return SimpleNamespace(data={'state': 'stale'})
            self.attempts[token]['outcome'] = args['p_outcome']
            if args['p_outcome'] == 'ready':
                self.cache[key] = {'translation': args['p_translation'], 'source_language': args['p_source_language']}
                return SimpleNamespace(data={'state': 'ready', **self.cache[key]})
            return SimpleNamespace(data={'state': 'finished'})
        return SimpleNamespace(execute=execute)


@pytest.fixture
def setup(monkeypatch):
    db, item, periods, calls = DB(), {'id': '123', 'caption': SOURCE}, [], []

    async def resolve(media_id, period=None):
        periods.append(period)
        if media_id != '123':
            raise HTTPException(404, 'Карусель не найдена')
        return dict(item)

    def provider(source):
        calls.append(source)
        return RUSSIAN, 'EN'

    monkeypatch.setattr(carousels, 'resolve_detail', resolve)
    monkeypatch.setattr(service, 'get_translation_db', lambda: db)
    monkeypatch.setattr(service, 'translate_to_ru_detailed', provider)
    app = FastAPI()
    app.include_router(carousels.router, prefix='/api')
    app.include_router(carousel_translation.router, prefix='/api')
    return SimpleNamespace(client=TestClient(app), db=db, item=item, periods=periods, calls=calls)


def post(s, **extra):
    return s.client.post(URL+'/translate', json={'period': '14d', 'source_hash': service.source_hash(s.item['caption']), **extra})


def test_translate_persists_and_reopening_across_periods_does_not_spend(setup):
    s = setup
    original = s.client.get(URL+'?period=14d')
    assert original.headers['cache-control'] == 'no-store'
    assert original.json()['caption_translation']['state'] == 'available'
    assert not s.calls and not s.db.calls
    first = post(s)
    assert first.status_code == 200 and first.json()['cached'] is False
    assert first.json()['caption_translation'] == {
        'source_hash': service.source_hash(SOURCE), 'state': 'ready', 'translation': RUSSIAN, 'source_language': 'en'}
    assert first.headers['cache-control'] == 'no-store'
    for period in ('7d', 'archive14d', '30d', '14d'):
        assert s.client.get(URL+'?period='+period).json()['caption_translation']['state'] == 'ready'
        assert post(s, period=period).json()['cached'] is True
    assert s.calls == [SOURCE]
    assert len(s.db.attempts) == 1
    assert s.client.get(URL).json()['caption'] == SOURCE


def test_hash_preserves_whitespace_and_changed_source_never_gets_old_cache(setup):
    s = setup
    old_hash = service.source_hash(SOURCE)
    assert post(s).status_code == 200
    s.item['caption'] = SOURCE+'\n'
    assert service.source_hash(s.item['caption']) != old_hash
    assert s.client.get(URL).json()['caption_translation']['state'] == 'available'
    error = post(s, source_hash=old_hash)
    assert error.status_code == 409 and error.json()['code'] == 'source_changed'
    assert s.calls == [SOURCE] and len(s.db.attempts) == 1
    assert post(s).status_code == 200
    assert len(s.db.cache) == 2


@pytest.mark.parametrize('source,code,status', [
    ('', 'source_missing', 409), ('   ', 'source_missing', 409),
    ('Русский текст не требует перевода.', 'translation_not_needed', 409),
    ('OK 🎉', 'translation_not_needed', 409), ('x'*12001, 'source_too_long', 413),
])
def test_unneeded_or_too_long_source_never_reserves(setup, source, code, status):
    s = setup
    s.item['caption'] = source
    response = post(s)
    assert response.status_code == status and response.json()['code'] == code
    assert s.calls == [] and s.db.calls == []


@pytest.mark.parametrize('body', [
    {}, {'source_hash': 'abc'}, {'source_hash': 'A'*64},
    {'source_hash': '0'*64, 'text': 'client-controlled source'},
    {'source_hash': '0'*64, 'period': 'selected'}, {'source_hash': '0'*64, 'snapshot': {}},
])
def test_request_does_not_accept_arbitrary_source(setup, body):
    s = setup
    assert s.client.post(URL+'/translate', json=body).status_code == 422
    assert not s.calls and not s.db.calls


def test_invalid_id_and_not_found_are_not_translated(setup):
    s = setup
    for media_id, status in (('invalid', 422), ('999', 404)):
        r = s.client.post(f'/api/radar/carousels/{media_id}/translate', json={'source_hash': service.source_hash(SOURCE)})
        assert r.status_code == status
    assert not s.calls and not s.db.calls


def test_unavailable_database_fails_open_for_details_closed_for_post(setup):
    s = setup
    s.db.fail = True
    detail = s.client.get(URL)
    assert detail.status_code == 200 and detail.json()['caption'] == SOURCE
    assert detail.json()['caption_translation']['state'] == 'unavailable'
    error = post(s)
    assert error.status_code == 503 and error.json()['code'] == 'translation_unavailable'
    assert 'закрытый' not in error.text and not s.calls


def test_missing_server_key_is_checked_without_changing_anon_client(setup, monkeypatch):
    s = setup
    translation_db.get_translation_db.cache_clear()
    monkeypatch.setattr(translation_db, 'settings', SimpleNamespace(supabase_service_role_key=''))
    monkeypatch.setattr(service, 'get_translation_db', translation_db.get_translation_db)
    assert s.client.get(URL).json()['caption_translation']['state'] == 'unavailable'
    assert post(s).status_code == 503
    assert not s.calls


@pytest.mark.parametrize('state,status,code', [
    ('in_progress', 409, 'translation_in_progress'), ('limited', 429, 'translation_rate_limited'),
])
def test_shared_reservation_stops_provider_and_exposes_retry_after(setup, state, status, code):
    s = setup
    s.db.reserve_state = state
    r = post(s)
    assert r.status_code == status and r.json()['code'] == code
    assert int(r.headers['retry-after']) > 0 and not s.calls


@pytest.mark.parametrize('failure,status,code,outcome', [
    (TranslationQuotaExceededError('закрытый ответ'), 429, 'translation_quota_exceeded', 'quota'),
    (TranslationFailedError('unchanged', 'закрытый ответ'), 502, 'translation_failed', 'failed'),
    (RuntimeError('закрытый ответ'), 502, 'translation_failed', 'failed'),
])
def test_provider_errors_keep_charge_without_cached_bad_result(setup, monkeypatch, failure, status, code, outcome):
    s = setup
    def fail(source):
        raise failure
    monkeypatch.setattr(service, 'translate_to_ru_detailed', fail)
    r = post(s)
    assert r.status_code == status and r.json()['code'] == code
    assert 'закрытый' not in r.text and not s.db.cache
    assert [row['outcome'] for row in s.db.attempts.values()] == [outcome]


def test_invalid_cache_is_replaced_and_bad_new_translation_is_never_saved(setup, monkeypatch):
    s = setup
    key = '123', service.source_hash(SOURCE)
    s.db.cache[key] = {'translation': '<html>error</html>', 'source_language': None}
    assert post(s).status_code == 200 and s.db.cache[key]['translation'] == RUSSIAN
    s.item['caption'] = SOURCE+' altered'
    monkeypatch.setattr(service, 'translate_to_ru_detailed', lambda _: (SOURCE, None))
    assert post(s).status_code == 502
    assert len(s.db.cache) == 1


def test_changed_during_provider_does_not_publish_or_translate_new_caption(setup, monkeypatch):
    s = setup
    def provider(source):
        s.calls.append(source)
        s.item['caption'] = SOURCE+' new version'
        return RUSSIAN, None
    monkeypatch.setattr(service, 'translate_to_ru_detailed', provider)
    r = post(s)
    assert r.status_code == 409 and r.json()['code'] == 'source_changed'
    assert not s.db.cache and s.calls == [SOURCE]
    assert [a['outcome'] for a in s.db.attempts.values()] == ['source_changed']


@pytest.mark.parametrize('failure', ['write', 'stale'])
def test_save_failure_or_lost_lease_never_returns_unsaved_success(setup, failure):
    s = setup
    s.db.fail_finish = failure == 'write'
    s.db.stale = failure == 'stale'
    r = post(s)
    assert r.status_code == (503 if failure == 'write' else 409)
    assert not s.db.cache and s.calls == [SOURCE]


@pytest.mark.parametrize('failure', [HTTPException(503, 'Сохранённый источник временно недоступен'),
                                     RuntimeError('закрытый ответ источника')])
def test_source_unavailability_prevents_reservation(setup, monkeypatch, failure):
    s = setup
    async def unavailable(*args):
        raise failure
    monkeypatch.setattr(carousels, 'resolve_detail', unavailable)
    r = post(s)
    assert r.status_code == 503 and r.json()['code'] == 'carousel_source_unavailable'
    assert 'закрытый' not in r.text
    assert not s.calls and not s.db.calls


def test_cancelled_await_does_not_release_still_running_provider(setup, monkeypatch):
    s = setup
    entered, release = Event(), Event()
    def provider(source):
        entered.set()
        assert release.wait(5)
        return RUSSIAN, None
    monkeypatch.setattr(service, 'translate_to_ru_detailed', provider)
    async def check():
        task = asyncio.create_task(service.translate('123', '14d', service.source_hash(SOURCE)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(s.db.attempts) == 1
            assert all('outcome' not in row for row in s.db.attempts.values())
            assert not s.db.cache
        finally:
            release.set()
    asyncio.run(check())
