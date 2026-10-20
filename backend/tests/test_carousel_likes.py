"""Контракт общего отбора проверяется без сети и настоящей базы."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.carousel_likes import router
from app.services import carousel_likes as service


def saved(identity='123', active=True):
    return {'instagram_media_id': identity, 'active': active, 'selected_at': '2026-10-20T10:00:00Z',
            'source_period': '7d', 'snapshot': {'id': identity, 'media_type': 'CAROUSEL_ALBUM',
            'username': 'alice', 'caption': 'Сохранённый текст', 'permalink': 'https://www.instagram.com/p/test/'}}


def test_oversized_metric_does_not_break_snapshot_reading():
    raw = saved()['snapshot']
    raw['likes'] = 10 ** 400
    assert service.snapshot(raw, '123')['likes'] is None


class FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.fail = False

    def table(self, name):
        assert name == 'carousel_likes'
        self.filters = {}
        self.bounds = (0, len(self.rows))
        self.rpc_args = None
        return self

    def select(self, columns):
        assert columns == service.COLUMNS
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, start, end):
        self.bounds = (start, end + 1)
        self.calls.append(('range', start, end))
        return self

    def limit(self, count):
        self.bounds = (0, count)
        return self

    def rpc(self, name, args):
        assert name == 'set_carousel_like'
        self.calls.append(('rpc', args))
        self.rpc_args = args
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError('Внутренняя ошибка с закрытыми данными')
        if self.rpc_args:
            args = self.rpc_args
            row = next((row for row in self.rows if row['instagram_media_id'] == args['p_id']), None)
            if not args['p_selected']:
                if row:
                    row['active'] = False
                return SimpleNamespace(data={'id': args['p_id'], 'selected': False})
            if row is None:
                row = saved(args['p_id'])
                self.rows.append(row)
            row.update(active=True, snapshot=args['p_snapshot'], source_period=args['p_period'])
            return SimpleNamespace(data={'id': row['instagram_media_id'], **row})
        rows = [row for row in self.rows if all(row[key] == value for key, value in self.filters.items())]
        return SimpleNamespace(data=rows[slice(*self.bounds)])


@pytest.fixture
def setup(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, 'get_db', lambda: db)
    async def unavailable(*args):
        raise HTTPException(404, 'Нет в отчёте')
    monkeypatch.setattr(service.carousels, 'detail', unavailable)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    return TestClient(app), db


URL = '/api/radar/carousel-likes'


def test_paginates_all_active_rows_and_disables_cache(setup):
    client, db = setup
    db.rows = [saved(str(i)) for i in range(1001)] + [saved('9999', False)]
    response = client.get(URL)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['total'] == 1001
    assert ('range', 1000, 1999) in db.calls
    assert all('active' not in item for item in response.json()['items'])


def test_select_uses_report_whitelist_other_period_and_idempotent_rpc(setup, monkeypatch):
    client, db = setup
    periods = []
    async def detail(identity, period):
        periods.append(period)
        if period == '7d':
            raise HTTPException(404, 'Нет')
        return {**saved(identity)['snapshot'], 'secret_extra': 'не сохранять',
                'media_url': 'https://evil.test/payload', 'likes': True}
    monkeypatch.setattr(service.carousels, 'detail', detail)
    for _ in range(2):
        response = client.put(URL + '/123', json={'period': '7d'})
        assert response.status_code == 200
        item = response.json()['item']
        assert item['source_period'] == '14d'
        assert item['snapshot']['media_url'] is None
        assert item['snapshot']['likes'] is None
        assert 'secret_extra' not in item['snapshot']
    assert periods == ['7d', '14d', '7d', '14d']
    assert len(db.rows) == 1


def test_restore_saved_snapshot_and_soft_delete(setup):
    client, db = setup
    db.rows = [saved(active=False)]
    response = client.put(URL + '/123', json={'period': '14d'})
    assert response.status_code == 200
    assert response.json()['item']['source_period'] == '7d'
    for _ in range(2):
        response = client.delete(URL + '/123')
        assert response.json() == {'id': '123', 'selected': False}
        assert response.headers['cache-control'] == 'no-store'
    assert db.rows[0]['snapshot']['caption'] == 'Сохранённый текст'
    assert client.get(URL).json()['total'] == 0


def test_validation_and_unknown_item_never_write(setup):
    client, db = setup
    for identity in ('abc', '1' * 41, '１２３'):
        assert client.put(URL + '/' + identity, json={'period': '7d'}).status_code == 422
        assert client.delete(URL + '/' + identity).status_code == 422
    for body in ({'period': '7d', 'snapshot': {}}, {'period': '30d'}, {}):
        assert client.put(URL + '/123', json=body).status_code == 422
    assert client.put(URL + '/123', json={'period': '7d'}).status_code == 404
    assert not any(call[0] == 'rpc' for call in db.calls)


def test_database_errors_do_not_expose_details_or_retry_writes(setup, monkeypatch):
    client, db = setup
    async def detail(identity, period):
        return saved(identity)['snapshot']
    monkeypatch.setattr(service.carousels, 'detail', detail)
    db.fail = True
    response = client.put(URL + '/123', json={'period': '7d'})
    assert response.status_code == 503
    assert 'закрытыми' not in response.text
    assert len(db.calls) == 1


def test_invalid_snapshot_not_written(setup, monkeypatch):
    client, db = setup
    async def detail(identity, period):
        return {**saved(identity)['snapshot'], 'caption': 'x' * 65537}
    monkeypatch.setattr(service.carousels, 'detail', detail)
    assert client.put(URL + '/123', json={'period': '7d'}).status_code == 422
    assert db.calls == []


def test_long_caption_is_preserved_and_offline_reports_restore_saved(setup, monkeypatch):
    client, db = setup
    caption = 'Я' * 12000
    async def detail(identity, period):
        return {**saved(identity)['snapshot'], 'caption': caption}
    monkeypatch.setattr(service.carousels, 'detail', detail)
    assert client.put(URL + '/123', json={'period': '7d'}).json()['item']['snapshot']['caption'] == caption
    async def offline(*args):
        raise HTTPException(503, 'Отчёт недоступен')
    monkeypatch.setattr(service.carousels, 'detail', offline)
    client.delete(URL + '/123')
    assert client.put(URL + '/123', json={'period': '14d'}).json()['item']['snapshot']['caption'] == caption
    assert client.put(URL + '/456', json={'period': '14d'}).status_code == 503


@pytest.mark.parametrize('requested,expected', [
    ('7d', ['7d', '14d', 'archive14d']),
    ('14d', ['14d', '7d', 'archive14d']),
    ('archive14d', ['archive14d', '7d', '14d']),
])
def test_fallback_checks_every_report_once_before_saved_restore(setup, monkeypatch, requested, expected):
    client, db = setup
    row = saved(active=False)
    row['source_period'] = 'archive14d'
    db.rows = [row]
    periods = []
    async def detail(identity, period):
        periods.append(period)
        raise HTTPException(404, 'Нет в отчёте')
    monkeypatch.setattr(service.carousels, 'detail', detail)
    response = client.put(URL + '/123', json={'period': requested})
    assert response.status_code == 200
    assert periods == expected
    assert response.json()['item']['source_period'] == 'archive14d'
    assert db.calls[-1][1]['p_period'] == 'archive14d'


@pytest.mark.parametrize('requested', ['7d', '14d', 'archive14d'])
def test_select_and_read_archive_snapshot(setup, monkeypatch, requested):
    client, db = setup
    periods = []
    async def detail(identity, period):
        periods.append(period)
        if period != 'archive14d':
            raise HTTPException(404, 'Нет в отчёте')
        return saved(identity)['snapshot']
    monkeypatch.setattr(service.carousels, 'detail', detail)
    response = client.put(URL + '/123', json={'period': requested})
    assert response.status_code == 200
    assert response.json()['item']['source_period'] == 'archive14d'
    assert periods[0] == requested
    assert len(periods) == len(set(periods))
    assert db.calls[-1][1]['p_period'] == 'archive14d'
    assert client.get(URL).json()['items'][0]['source_period'] == 'archive14d'


def test_fallback_keeps_unavailable_report_error_when_other_reports_miss(setup, monkeypatch):
    client, db = setup
    periods = []
    async def detail(identity, period):
        periods.append(period)
        raise HTTPException(503 if period == 'archive14d' else 404, 'Отчёт недоступен')
    monkeypatch.setattr(service.carousels, 'detail', detail)
    assert client.put(URL + '/123', json={'period': 'archive14d'}).status_code == 503
    assert periods == ['archive14d', '7d', '14d']
    assert not any(call[0] == 'rpc' for call in db.calls)
