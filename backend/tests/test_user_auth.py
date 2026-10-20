"""Контракт входа и изоляция пользовательских токенов без сети."""
import asyncio
from contextvars import copy_context
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import user_auth as api
from app.core import user_auth as core

USER = '2f59471e-02cc-42a7-b875-4418fe59f2cb'
SESSION = {'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600,
           'user': {'id': USER, 'email': 'alice@example.com', 'private_field': 'hidden'}}


@pytest.fixture
def client(monkeypatch):
    api._attempts.clear()
    monkeypatch.setattr(api, 'auth_request', AsyncMock(return_value=SESSION))
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    @app.get('/private')
    async def private(user_id: str = Depends(core.get_current_user_id)):
        return {'user_id': user_id, 'token': core.get_current_token()}
    return TestClient(app)


def test_start_normalizes_email_and_limits_resend(client):
    body = {'email': ' Alice@Example.com '}
    response = client.post('/api/auth/email/start', json=body)
    assert response.status_code == 204 and response.content == b''
    assert response.headers['cache-control'] == 'private, no-store'
    api.auth_request.assert_awaited_once_with('POST', 'otp', payload={'email': 'alice@example.com', 'create_user': True})
    assert client.post('/api/auth/email/start', json=body).status_code == 429


def test_verify_refresh_contract_and_private_headers(client):
    response = client.post('/api/auth/email/verify', json={'email': 'alice@example.com', 'code': '123456'})
    assert response.status_code == 200
    assert response.json() == {**SESSION, 'user': {'id': USER, 'email': 'alice@example.com'}}
    assert response.headers['cache-control'] == 'private, no-store'
    api.auth_request.assert_awaited_with('POST', 'verify', payload={'email': 'alice@example.com', 'token': '123456', 'type': 'email'})
    response = client.post('/api/auth/refresh', json={'refresh_token': 'old'})
    assert response.status_code == 200
    api.auth_request.assert_awaited_with('POST', 'token?grant_type=refresh_token', payload={'refresh_token': 'old'})


@pytest.mark.parametrize('path,body', [
    ('email/start', {'email': 'invalid'}),
    ('email/start', {'email': 'alice@example.com', 'user_id': USER}),
    ('email/verify', {'email': 'alice@example.com', 'code': 'abc'}),
    ('refresh', {'refresh_token': ''}),
])
def test_invalid_input(client, path, body):
    response = client.post('/api/auth/' + path, json=body)
    assert response.status_code == 422
    assert response.headers['cache-control'] == 'private, no-store'
    api.auth_request.assert_not_awaited()


def test_invalid_session_and_provider_errors_are_private(client):
    api.auth_request.return_value = {'access_token': 'secret'}
    response = client.post('/api/auth/refresh', json={'refresh_token': 'old'})
    assert response.status_code == 502 and 'secret' not in response.text
    api.auth_request.side_effect = HTTPException(401, 'Код истёк')
    response = client.post('/api/auth/email/verify', json={'email': 'alice@example.com', 'code': '123456'})
    assert response.status_code == 401
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('authorization', ['', 'Basic abc', 'Bearer', 'Bearer a b'])
def test_missing_or_malformed_bearer(client, authorization, monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(core, 'get_user', lookup)
    assert client.get('/private', headers={'Authorization': authorization}).status_code == 401
    lookup.assert_not_awaited()


def test_verified_uuid_and_invalid_user(client, monkeypatch):
    lookup = AsyncMock(return_value={'id': USER})
    monkeypatch.setattr(core, 'get_user', lookup)
    response = client.get('/private', headers={'Authorization': 'Bearer alice-token'})
    assert response.json() == {'user_id': USER, 'token': 'alice-token'}
    lookup.assert_awaited_once_with('alice-token')
    lookup.return_value = {'id': 'forged'}
    assert client.get('/private', headers={'Authorization': 'Bearer evil'}).status_code == 401
    lookup.return_value = {'id': USER, 'is_anonymous': True}
    assert client.get('/private', headers={'Authorization': 'Bearer anon'}).status_code == 401


def test_concurrent_request_tokens_are_isolated(monkeypatch):
    async def lookup(token):
        await asyncio.sleep(0)
        return {'id': USER}
    monkeypatch.setattr(core, 'get_user', lookup)
    async def task(token):
        request = Request({'type': 'http', 'headers': [(b'authorization', f'Bearer {token}'.encode())]})
        await core.get_current_user_id(request)
        await asyncio.sleep(0)
        return core.get_current_token()
    async def run():
        return await asyncio.gather(task('alice'), task('bob'))
    assert asyncio.run(run()) == ['alice', 'bob']
    with pytest.raises(HTTPException):
        copy_context().run(core.get_current_token)


@pytest.mark.parametrize('status,expected', [(401, 401), (429, 429), (500, 503)])
def test_upstream_errors_do_not_expose_response(monkeypatch, status, expected):
    async def request(*args, **kwargs):
        return httpx.Response(status, json={'message': 'sensitive upstream data'})
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(core.auth_request('GET', 'user', token='secret'))
    assert exc.value.status_code == expected
    assert 'sensitive' not in exc.value.detail


def test_get_user_uses_remote_verification(monkeypatch):
    call = AsyncMock(return_value={'id': USER})
    monkeypatch.setattr(core, 'auth_request', call)
    assert asyncio.run(core.get_user('access')) == {'id': USER}
    call.assert_awaited_once_with('GET', 'user', token='access')
