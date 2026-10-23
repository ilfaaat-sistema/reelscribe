"""Кэш и синхронная обработка обложки; только явный POST запускает модель."""
import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from app.api import carousels
from app.services.carousel_cover_image import ImageUnavailable, download_image
from app.services.carousel_cover_settings import get_settings
from app.services.carousel_cover_store import CoverStore
from app.services.carousel_cover_vertex import create_vertex_client, generate

logger = logging.getLogger(__name__)
STALE_RUNNING_SECONDS = 180


def item(media_id, row=None, status='missing'):
    row = row or {}
    status = row.get('status', status)
    if status == 'running' and row.get('created_at'):
        try:
            started = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - started).total_seconds() >= STALE_RUNNING_SECONDS:
                # Только представление: бюджет и слот зависшего вызова остаются занятыми.
                status = 'error'
        except (ValueError, TypeError):
            status = 'error'
    return {'id': media_id, 'status': status,
            'source_text': row.get('source_text'), 'text_ru': row.get('text_ru'),
            'retry_after': row.get('retry_after') if status == 'limit_reached' else None}


async def database(method, *args, **kwargs):
    try:
        return await asyncio.to_thread(method, *args, **kwargs)
    except Exception:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
        raise HTTPException(503, 'Хранилище переводов обложек временно недоступно') from None


async def read(ids):
    store = CoverStore()
    rows = await database(store.read, ids)
    budget = await database(store.budget)
    exhausted = budget if budget['status'] == 'limit_reached' else None
    cached = {row['media_id']: row for row in rows}
    return {'items': [item(identity, cached.get(identity, exhausted)) for identity in ids],
            'enabled': get_settings().enabled,
            'budget': {key: budget[key] for key in ('mode', 'pilot_limit', 'daily_limit', 'reset_at')}}


async def translate(media_id, period, *, oidc_token=None):
    store = CoverStore()
    rows = await database(store.read, [media_id])
    if rows:
        return {'item': item(media_id, rows[0])}
    if not get_settings().enabled:
        return {'item': item(media_id, status='disabled')}
    budget = await database(store.budget)
    if budget['status'] == 'limit_reached':
        return {'item': item(media_id, budget)}
    try:
        cover = await carousels.detail(media_id, period)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(422, 'Карусель отсутствует в выбранном наборе') from None
        raise
    # Только адрес существующей серверной записи, никаких URL из тела запроса.
    try:
        data, mime = await download_image(cover.get('media_url') or cover.get('thumbnail_url'))
    except ImageUnavailable:
        row = await database(store.unavailable, media_id)
        return {'item': item(media_id, row)}
    try:
        client = await asyncio.to_thread(create_vertex_client, oidc_token=oidc_token)
    except Exception:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
        raise HTTPException(503, 'Vertex для перевода обложек недоступен') from None
    try:
        row = await database(store.reserve, media_id, get_settings().vertex_model, hashlib.sha256(data).hexdigest())
        if row['status'] != 'running' or not row.get('claim_token'):
            return {'item': item(media_id, row)}
        started = time.monotonic()
        uncertain = False
        try:
            async with asyncio.timeout(125):
                result, usage = await generate(client, data, mime)
        except Exception as error:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
            from google.genai.errors import APIError
            from httpx import Response
            # Закрытый HTTP-ответ — завершённый вызов. Сетевой таймаут остаётся неизвестным.
            usage = {'error_type': type(error).__name__}
            code = getattr(error, 'code', None)
            if isinstance(code, int):
                usage['provider_status'] = code
            response = getattr(error, 'response', None)
            completed = (isinstance(error, APIError) and isinstance(response, Response)
                         and response.is_closed and isinstance(code, int) and 400 <= code < 600)
            usage['provider_response_complete'] = completed
            logger.warning('Сбой перевода обложки id=%s, type=%s, code=%s',
                           media_id, type(error).__name__, code if isinstance(code, int) else None)
            result, uncertain = {'status': 'error'}, not completed
        saved = await database(store.finish, media_id, row['claim_token'], result, usage,
                               int((time.monotonic() - started) * 1000), uncertain)
        return {'item': item(media_id, saved)}
    finally:
        try:
            await client.aio.aclose()
            client.close()
        except Exception:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
            logger.warning('Не удалось закрыть транспорт перевода обложки')
