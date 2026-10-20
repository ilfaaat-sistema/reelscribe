"""Проверка Supabase Auth без общего изменяемого состояния клиента."""
from contextvars import ContextVar
from uuid import UUID

import httpx
from fastapi import HTTPException, Request

from app.core.config import settings

_current_token: ContextVar[str] = ContextVar('current_user_access_token', default='')


def get_current_token() -> str:
    token = _current_token.get()
    if not token:
        raise HTTPException(401, 'Требуется вход', headers={'WWW-Authenticate': 'Bearer'})
    return token


async def auth_request(method: str, path: str, *, payload=None, token: str = '') -> dict:
    headers = {'apikey': settings.supabase_anon_key}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.request(
                method, f'{settings.supabase_url.rstrip("/")}/auth/v1/{path}',
                headers=headers, json=payload,
            )
    except httpx.HTTPError:
        raise HTTPException(503, 'Сервис входа временно недоступен') from None
    if response.status_code == 429:
        raise HTTPException(429, 'Слишком много попыток. Попробуйте позже', headers={'Retry-After': '60'})
    if response.status_code >= 500:
        raise HTTPException(503, 'Сервис входа временно недоступен')
    if response.is_error:
        raise HTTPException(401, 'Неверный или истёкший код либо сессия', headers={'WWW-Authenticate': 'Bearer'})
    if not response.content:
        return {}
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise HTTPException(502, 'Некорректный ответ сервиса входа') from None


async def get_user(token: str) -> dict:
    """Серверная проверка токена через Supabase Auth GET /user."""
    return await auth_request('GET', 'user', token=token)


async def get_current_user_id(request: Request) -> str:
    _current_token.set('')
    parts = request.headers.get('Authorization', '').split()
    if len(parts) != 2 or parts[0].lower() != 'bearer' or len(parts[1]) > 16384:
        raise HTTPException(401, 'Требуется вход', headers={'WWW-Authenticate': 'Bearer'})
    user = await get_user(parts[1])
    try:
        user_id = str(UUID(user['id']))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, 'Недействительная сессия', headers={'WWW-Authenticate': 'Bearer'}) from None
    if user.get('is_anonymous'):
        raise HTTPException(401, 'Требуется вход по электронной почте')
    _current_token.set(parts[1])
    return user_id
