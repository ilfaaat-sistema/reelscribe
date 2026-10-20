"""Проверки HTTP-контракта без БД и внешних запросов."""
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import carousel_selections as api

BASE = '/api/radar/carousel-selections'


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()

    async def authenticate(request: Request):
        token = request.headers.get('Authorization')
        if token not in ('Bearer alice', 'Bearer bob'):
            raise HTTPException(401, 'Требуется вход', headers={'WWW-Authenticate': 'Bearer'})
        return token.split()[1]

    app.dependency_overrides[api.get_current_user_id] = authenticate
    app.include_router(api.router, prefix='/api')
    return TestClient(app)


def headers(user='alice'):
    return {'Authorization': f'Bearer {user}'}


def test_all_routes_require_verified_user(client):
    for method, path, kwargs in [
        ('get', '', {}), ('post', '/status', {'json': {'ids': ['1']}}),
        ('get', '/1', {}),
        ('put', '/1', {'json': {'source_period': '14d', 'expected_revision': 0}}),
        ('delete', '/1?expected_revision=0', {}),
    ]:
        response = getattr(client, method)(BASE + path, **kwargs)
        assert response.status_code == 401
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['www-authenticate'] == 'Bearer'


def test_list_contract_defaults_and_explicit_pagination(client, monkeypatch):
    calls = []
    def listing(*args):
        calls.append(args)
        return {'items': [], 'total': 0, 'selected_total': 9}
    monkeypatch.setattr(api.service, 'list_selections', listing)
    response = client.get(BASE, headers=headers())
    assert response.json() == {'items': [], 'total': 0, 'selected_total': 9}
    assert calls[-1] == ('alice', '', 'selected_at', 'desc', 50, 0)
    assert response.headers['cache-control'] == 'private, no-store'
    response = client.get(BASE + '/?q=%40Alice&sort=likes&order=asc&limit=100&offset=100', headers=headers('bob'))
    assert response.status_code == 200
    assert calls[-1] == ('bob', '@Alice', 'likes', 'asc', 100, 100)


@pytest.mark.parametrize('query', ['q=' + 'x' * 201, 'sort=views', 'order=random', 'limit=75', 'offset=-1'])
def test_invalid_list_filters(client, query):
    response = client.get(BASE + '?' + query, headers=headers())
    assert response.status_code == 422
    assert response.headers['cache-control'] == 'private, no-store'


def test_status_static_route_and_user_scope(client, monkeypatch):
    def status(user, ids):
        assert user == 'bob' and ids == ['1', '2']
        return {'states': [{'id': '1', 'selected': False, 'revision': 0}], 'selected_total': 0}
    monkeypatch.setattr(api.service, 'selection_status', status)
    response = client.post(BASE + '/status', headers=headers('bob'), json={'ids': ['1', '2']})
    assert response.status_code == 200 and response.json()['states'][0]['revision'] == 0


def test_availability_distinguishes_missing_item_from_unavailable_report(client, monkeypatch):
    async def report(period):
        assert period == '14d'
        return {'items': [{'id': '1', 'media_type': 'CAROUSEL_ALBUM'},
                          {'id': '2', 'media_type': 'VIDEO'}]}
    monkeypatch.setattr(api, '_report', report)
    response = client.post(BASE + '/availability', headers=headers(),
                           json={'ids': ['1', '2'], 'period': '14d'})
    assert response.status_code == 200
    assert response.json() == {'status': 'known', 'states': [
        {'id': '1', 'in_report': True}, {'id': '2', 'in_report': False}]}
    async def unavailable(period):
        raise HTTPException(503, 'Сбор недоступен')
    monkeypatch.setattr(api, '_report', unavailable)
    response = client.post(BASE + '/availability', headers=headers(), json={'ids': ['1']})
    assert response.json() == {'status': 'unknown', 'states': []}


@pytest.mark.parametrize('body', [{'ids': ['1'] * 101}, {'ids': ['x']}, {'ids': [1]},
                                 {'ids': ['1' * 41]}, {'ids': ['1'], 'user_id': 'bob'}])
def test_status_rejects_invalid_ids_and_owner_injection(client, body):
    assert client.post(BASE + '/status', headers=headers(), json=body).status_code == 422


def test_mutations_forward_only_verified_owner_and_expected_revision(client, monkeypatch):
    calls = []
    for name in ('get_selection', 'select_carousel', 'remove_selection'):
        monkeypatch.setattr(api.service, name, lambda *args: calls.append(args) or {'ok': True})
    assert client.get(BASE + '/123', headers=headers('bob')).status_code == 200
    assert calls[-1] == ('bob', '123')
    assert client.put(BASE + '/123', headers=headers(), json={'source_period': '14d', 'expected_revision': 4}).status_code == 200
    assert calls[-1] == ('alice', '123', '14d', 4)
    assert client.delete(BASE + '/123?expected_revision=5', headers=headers()).status_code == 200
    assert calls[-1] == ('alice', '123', 5)


@pytest.mark.parametrize('body', [
    {'source_period': '30d', 'expected_revision': 0}, {'source_period': '7d'},
    {'source_period': '7d', 'expected_revision': -1},
    {'source_period': '7d', 'expected_revision': True},
    {'source_period': '7d', 'expected_revision': '0'},
    {'source_period': '7d', 'expected_revision': 0, 'snapshot': {'caption': 'Подмена'}},
    {'source_period': '7d', 'expected_revision': 0, 'user_id': 'bob'},
])
def test_put_validation(client, body):
    assert client.put(BASE + '/1', headers=headers(), json=body).status_code == 422


def test_invalid_detail_and_delete_revision(client):
    for path in ('/abc', '/' + '1' * 41):
        assert client.get(BASE + path, headers=headers()).status_code == 422
    for query in ('', '?expected_revision=-1', '?expected_revision=no'):
        assert client.delete(BASE + '/1' + query, headers=headers()).status_code == 422


@pytest.mark.parametrize('code', [401, 404, 409, 422, 503])
def test_service_errors_preserve_status_detail_and_private_cache(client, monkeypatch, code):
    detail = {'message': 'Ошибка', 'state': {'id': '1', 'selected': True, 'revision': 8}}
    def fail(*args):
        raise api.service.CarouselSelectionError(code, detail)
    monkeypatch.setattr(api.service, 'get_selection', fail)
    response = client.get(BASE + '/1', headers=headers())
    assert response.status_code == code and response.json() == {'detail': detail}
    assert response.headers['cache-control'] == 'private, no-store'
