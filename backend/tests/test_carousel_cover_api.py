from unittest.mock import AsyncMock

from app.api import carousel_cover_translations as api
from fastapi import FastAPI
from fastapi.testclient import TestClient


def client():
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    return TestClient(app)


def test_read_validation_and_batch_deduplication(monkeypatch):
    read = AsyncMock(return_value={'items': [], 'enabled': False})
    monkeypatch.setattr(api.service, 'read', read)
    c = client()
    response = c.get('/api/radar/carousel-cover-translations', params={'ids': '1,1,2'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    read.assert_awaited_once_with(['1', '2'])
    for ids in ('', 'https://example.com', '1,' + ','.join(str(n) for n in range(100))):
        assert c.get('/api/radar/carousel-cover-translations', params={'ids': ids}).status_code == 422


def test_post_rejects_client_url_invalid_id_and_period(monkeypatch):
    translate = AsyncMock(return_value={'item': {'id': '1', 'status': 'disabled'}})
    monkeypatch.setattr(api.service, 'translate', translate)
    c = client()
    base = '/api/radar/carousel-cover-translations/'
    assert c.post(base + '1', json={'period': '7d', 'url': 'https://x.fbcdn.net/a'}).status_code == 422
    assert c.post(base + 'abc', json={'period': '7d'}).status_code == 422
    assert c.post(base + '1', json={'period': '1d'}).status_code == 422
    translate.assert_not_awaited()
    response = c.post(base + '1', json={'period': 'archive14d'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert translate.await_count == 1
    assert translate.await_args.args == ('1', 'archive14d')
    assert translate.await_args.kwargs.get('oidc_token') is None



def test_api_preserves_budget_metadata_and_retry_timestamp(monkeypatch):
    deadline = '2030-01-01T21:00:00Z'
    value = {'id': '1', 'status': 'limit_reached', 'source_text': None,
             'text_ru': None, 'retry_after': deadline}
    budget = {'mode': 'live', 'pilot_limit': 20, 'daily_limit': 500, 'reset_at': deadline}
    monkeypatch.setattr(api.service, 'read', AsyncMock(return_value={
        'items': [value], 'enabled': True, 'budget': budget}))
    monkeypatch.setattr(api.service, 'translate', AsyncMock(return_value={'item': value}))
    c = client()
    response = c.get('/api/radar/carousel-cover-translations', params={'ids': '1'})
    assert response.status_code == 200
    assert response.json() == {'items': [value], 'enabled': True, 'budget': budget}
    response = c.post('/api/radar/carousel-cover-translations/1', json={'period': '7d'})
    assert response.status_code == 200
    assert response.json() == {'item': value}
    assert response.headers['cache-control'] == 'no-store'
