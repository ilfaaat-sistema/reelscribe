"""Вход по коду из письма; локальные лимиты дополняют ограничения Supabase."""
import hashlib
import re
import time
from collections import OrderedDict
from threading import Lock
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.user_auth import auth_request, get_current_token, get_current_user_id


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={'detail': 'Некорректные данные входа'},
                                    headers={'Cache-Control': 'private, no-store'})
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), 'Cache-Control': 'private, no-store'}
                raise
            response.headers['Cache-Control'] = 'private, no-store'
            return response
        return handler


router = APIRouter(prefix='/auth', tags=['auth'], route_class=PrivateRoute)
_attempts: OrderedDict[str, list[float]] = OrderedDict()
_limit_lock = Lock()


def limit(request: Request, action: str, identity: str = ''):
    """Лимит одного процесса; адреса прокси из заголовков не доверяются."""
    now = time.monotonic()
    ip = request.client.host if request.client else 'unknown'
    rules = [(f'{action}:ip:{ip}', 30, 60)]
    if identity:
        digest = hashlib.sha256(identity.encode()).hexdigest()
        rules.append((f'{action}:identity:{digest}', 1 if action == 'start' else 8, 60))
    with _limit_lock:
        for key, maximum, window in rules:
            attempts = [stamp for stamp in _attempts.get(key, []) if now - stamp < window]
            _attempts[key] = attempts
            _attempts.move_to_end(key)
            if len(attempts) >= maximum:
                raise HTTPException(429, 'Слишком много попыток. Попробуйте через минуту', headers={'Retry-After': '60'})
        for key, _, _ in rules:
            _attempts[key].append(now)
        while len(_attempts) > 10000:
            _attempts.popitem(last=False)


class EmailBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=254)

    @field_validator('email')
    @classmethod
    def validate_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Укажите корректный email')
        return value


class VerifyBody(EmailBody):
    code: str = Field(pattern=r'^[0-9]{6,10}$')


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    refresh_token: str = Field(min_length=1, max_length=16384)


def session_response(data: dict) -> dict:
    try:
        user = data['user']
        user_id = str(UUID(user['id']))
        access, refresh = data['access_token'], data['refresh_token']
        expiry = data['expires_in']
        email = user['email']
        if not all(isinstance(v, str) and v for v in (access, refresh, email)) or not isinstance(expiry, int) or expiry <= 0:
            raise ValueError
        return {'access_token': access, 'refresh_token': refresh, 'expires_in': expiry,
                'user': {'id': user_id, 'email': email}}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(502, 'Некорректный ответ сервиса входа') from None


@router.post('/email/start', status_code=204)
async def start(body: EmailBody, request: Request):
    limit(request, 'start', body.email)
    await auth_request('POST', 'otp', payload={'email': body.email, 'create_user': True})
    return Response(status_code=204)


@router.post('/email/verify')
async def verify(body: VerifyBody, request: Request):
    limit(request, 'verify', body.email)
    return session_response(await auth_request('POST', 'verify', payload={'email': body.email, 'token': body.code, 'type': 'email'}))


@router.post('/refresh')
async def refresh(body: RefreshBody, request: Request):
    limit(request, 'refresh')
    return session_response(await auth_request('POST', 'token?grant_type=refresh_token', payload={'refresh_token': body.refresh_token}))


@router.post('/logout', status_code=204)
async def logout(user_id: str = Depends(get_current_user_id)):
    await auth_request('POST', 'logout?scope=local', token=get_current_token())
    return Response(status_code=204)
