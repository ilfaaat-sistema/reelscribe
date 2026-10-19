"""Публичный перевод: только сохранённый источник, кэш и предохранители без БД/сети."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import translation
from app.pipeline.translate import TranslationFailedError, TranslationQuotaExceededError

SOURCE = 'This is a sufficiently long English sentence for a useful translation.'
RUSSIAN = 'Это достаточно длинное английское предложение для полезного перевода.'


class Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.update_data = None

    def select(self, _columns):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def update(self, data):
        self.update_data = data
        return self

    def execute(self):
        found = [row for row in self.rows if all(row.get(k) == v for k, v in self.filters)]
        if self.update_data is not None:
            for row in found:
                row.update(self.update_data)
        return SimpleNamespace(data=found)


class DB:
    def __init__(self, reel_id):
        self.rows = {
            'reels': [{'id': reel_id, 'caption': SOURCE, 'caption_ru': None, 'caption_lang': None}],
            'transcripts': [{'reel_id': reel_id, 'text': SOURCE, 'text_ru': None, 'language': 'en'}],
        }

    def table(self, name):
        return Query(self.rows[name])


@pytest.fixture
def api(monkeypatch):
    reel_id = str(uuid4())
    db = DB(reel_id)
    monkeypatch.setattr(translation, 'get_db', lambda: db)
    translation._attempts.clear()
    translation._in_flight.clear()
    app = FastAPI()
    app.include_router(translation.router, prefix='/api')
    return TestClient(app), db, reel_id


def test_caption_translates_stored_source_and_reuses_cache(api, monkeypatch):
    client, db, reel_id = api
    calls = []

    def fake_translate(text):
        calls.append(text)
        return RUSSIAN, 'en'

    monkeypatch.setattr(translation, 'translate_to_ru_detailed', fake_translate)
    path = f'/api/reels/{reel_id}/translate'
    first = client.post(path, json={'field': 'caption'})
    second = client.post(path, json={'field': 'caption'})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {'field': 'caption', 'translation': RUSSIAN, 'caption_lang': 'en'}
    assert calls == [SOURCE]
    assert db.rows['reels'][0]['caption_ru'] == RUSSIAN


def test_transcript_uses_own_source_and_column(api, monkeypatch):
    client, db, reel_id = api
    monkeypatch.setattr(translation, 'translate_to_ru_detailed', lambda text: (RUSSIAN, 'en'))
    response = client.post(f'/api/reels/{reel_id}/translate', json={'field': 'transcript'})
    assert response.status_code == 200
    assert response.json() == {'field': 'transcript', 'translation': RUSSIAN}
    assert db.rows['transcripts'][0]['text_ru'] == RUSSIAN
    assert db.rows['reels'][0]['caption_ru'] is None


def test_rejects_invalid_request_missing_or_unneeded_source(api, monkeypatch):
    client, db, reel_id = api
    path = f'/api/reels/{reel_id}/translate'
    monkeypatch.setattr(translation, 'translate_to_ru_detailed', lambda _: pytest.fail('переводчик не должен вызываться'))
    assert client.post('/api/reels/not-a-uuid/translate', json={'field': 'caption'}).status_code == 422
    assert client.post(path, json={'field': 'arbitrary'}).status_code == 422
    assert client.post(f'/api/reels/{uuid4()}/translate', json={'field': 'caption'}).status_code == 404
    db.rows['reels'][0]['caption'] = 'Привет, это уже русский текст.'
    assert client.post(path, json={'field': 'caption'}).status_code == 409
    db.rows['reels'][0]['caption'] = ''
    assert client.post(path, json={'field': 'caption'}).status_code == 409


def test_size_and_rate_guards_prevent_translate_call(api, monkeypatch):
    client, db, reel_id = api
    path = f'/api/reels/{reel_id}/translate'
    monkeypatch.setattr(translation, 'translate_to_ru_detailed', lambda _: pytest.fail('переводчик не должен вызываться'))
    db.rows['reels'][0]['caption'] = SOURCE * 200
    assert client.post(path, json={'field': 'caption'}).status_code == 413
    db.rows['reels'][0]['caption'] = SOURCE
    translation._in_flight.add((reel_id, 'caption'))
    assert client.post(path, json={'field': 'caption'}).status_code == 409
    translation._in_flight.clear()
    translation._attempts.extend([translation.time.monotonic()] * translation.MAX_ATTEMPTS_PER_HOUR)
    assert client.post(path, json={'field': 'caption'}).status_code == 429


@pytest.mark.parametrize('failure,expected', [
    (TranslationQuotaExceededError('secret quota response'), 429),
    (TranslationFailedError('no_cyrillic', 'secret translator response'), 502),
    (RuntimeError('secret translator response'), 502),
])
def test_translator_failures_do_not_expose_details(api, monkeypatch, failure, expected):
    client, db, reel_id = api

    def fail(_):
        raise failure

    monkeypatch.setattr(translation, 'translate_to_ru_detailed', fail)
    response = client.post(f'/api/reels/{reel_id}/translate', json={'field': 'caption'})
    assert response.status_code == expected
    assert 'secret' not in response.text
    assert db.rows['reels'][0]['caption_ru'] is None
    assert not translation._in_flight


def test_invalid_cached_translation_is_replaced(api, monkeypatch):
    client, db, reel_id = api
    db.rows['reels'][0]['caption_ru'] = '<html>bad gateway</html>'
    monkeypatch.setattr(translation, 'translate_to_ru_detailed', lambda _: (RUSSIAN, 'en'))
    response = client.post(f'/api/reels/{reel_id}/translate', json={'field': 'caption'})
    assert response.status_code == 200
    assert db.rows['reels'][0]['caption_ru'] == RUSSIAN


def test_changed_source_is_not_overwritten(api, monkeypatch):
    client, db, reel_id = api

    def fake_translate(_):
        db.rows['reels'][0]['caption'] = 'A different English source was saved during translation.'
        return RUSSIAN, 'en'

    monkeypatch.setattr(translation, 'translate_to_ru_detailed', fake_translate)
    response = client.post(f'/api/reels/{reel_id}/translate', json={'field': 'caption'})
    assert response.status_code == 409
    assert db.rows['reels'][0]['caption_ru'] is None
    assert not translation._in_flight
