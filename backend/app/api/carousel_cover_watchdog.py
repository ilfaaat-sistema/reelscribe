"""Публичный минимальный сигнал закрытого server-side контроллера."""
import json

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.services import carousel_cover_watchdog as service

router = APIRouter(prefix='/radar/carousel-cover-background', tags=['carousels'])


@router.get('/status')
def background_status(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return service.status()


@router.post('/tick')
async def background_tick(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    if request.query_params:
        raise HTTPException(422, 'Параметры tick запрещены')
    raw = b''
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 64:
            raise HTTPException(422, 'Допустимо только пустое тело запроса')
    if raw.strip():
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'Допустимо только пустое тело запроса') from None
        if not isinstance(value, dict) or value:
            raise HTTPException(422, 'Допустимо только пустое тело запроса')
    return await run_in_threadpool(service.tick)
