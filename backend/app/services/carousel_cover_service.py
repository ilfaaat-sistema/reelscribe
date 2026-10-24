"""Кэш и синхронная обработка обложки; только явный POST запускает модель."""
import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.api import carousels
from app.services.carousel_cover_image import ImageUnavailable, download_image, normalize_image, public_cover_url
from app.services.carousel_cover_settings import get_settings
from app.services.carousel_cover_store import CoverStore
from app.services.carousel_cover_vertex import PROMPT_REVISION, create_vertex_client, generate, uses_russian_alphabet

logger = logging.getLogger(__name__)
STALE_RUNNING_SECONDS = 600
# 600 > maxDuration 300 + serverTimeout 120 + запас 120 секунд.


def item(media_id, row=None, status='missing', *, legacy=False):
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
    reason_code = row.get('reason_code')
    if (legacy and status == 'ready'
            and (row.get('usage') or {}).get('prompt_revision') in
            (None, 'cover-readable-v1', 'cover-readable-v2', 'cover-readable-v3', 'cover-readable-v4')
            and uses_russian_alphabet(row.get('source_text'))
            and isinstance(row.get('text_ru'), str)
            and row['source_text'].strip() == row['text_ru'].strip()):
        status, reason_code = 'not_needed', 'unchanged_source'
    return {'id': media_id, 'status': status,
            'reason_code': reason_code,
            'attempt_recorded': bool(row.get('attempt_recorded') or row.get('claim_token')),
            'source_text': row.get('source_text'), 'text_ru': '' if status == 'not_needed' else row.get('text_ru'),
            'retry_after': (row.get('retry_after') or (row.get('usage') or {}).get('retry_after'))
            if status == 'limit_reached' or row.get('reason_code') == 'provider_rate_limit' else None}


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
    return {'items': [item(identity, cached.get(identity, exhausted), legacy=True) for identity in ids],
            'enabled': get_settings().enabled,
            'budget': {key: budget[key] for key in ('mode', 'pilot_limit', 'daily_limit', 'reset_at',
                                                       'attempts', 'daily_attempts', 'active_slots', 'available_slots') if key in budget}}


async def translate(media_id, period, *, oidc_token=None):
    store = CoverStore()
    rows = await database(store.read, [media_id])
    if rows:
        return {'item': item(media_id, rows[0], legacy=True), 'attempt_reserved': False}
    if not get_settings().enabled:
        return {'item': item(media_id, status='disabled'), 'attempt_reserved': False}
    budget = await database(store.budget)
    if budget['status'] == 'limit_reached':
        return {'item': item(media_id, budget), 'attempt_reserved': False}
    try:
        cover = await carousels.detail(media_id, period)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(422, 'Карусель отсутствует в выбранном наборе') from None
        raise
    # Только адреса серверных записей: клиент не передаёт URL или refresh-параметры.
    override = await database(store.read_source_override, media_id)
    override_sources = (
        [('media_url', override.get('media_url')), ('thumbnail_url', override.get('thumbnail_url'))]
        if override and str(override.get('media_id')) == str(media_id) else []
    )
    image_normalization = 'jpeg2048'
    failures = []
    source_kind = None
    sources = override_sources + [
        ('media_url', cover.get('media_url')), ('thumbnail_url', cover.get('thumbnail_url')),
        ('public_first_cover', public_cover_url(cover))]
    for kind, url in sources:
        if not url:
            continue
        try:
            data, mime = await download_image(url)
            data, mime = await asyncio.to_thread(normalize_image, data, mime)
            source_kind = kind
            break
        except ImageUnavailable as error:
            failures.append((kind, error.reason_code))
    if source_kind is None:
        kind, reason = failures[-1] if failures else (None, 'permanent_missing')
        row = await database(store.unavailable, media_id, reason, kind)
        return {'item': item(media_id, row), 'attempt_reserved': False}
    try:
        client = await asyncio.to_thread(create_vertex_client, oidc_token=oidc_token)
    except Exception:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
        raise HTTPException(503, 'Vertex для перевода обложек недоступен') from None
    try:
        row = await database(store.reserve, media_id, get_settings().vertex_model, hashlib.sha256(data).hexdigest())
        if row['status'] != 'running' or not row.get('claim_token'):
            return {'item': item(media_id, row), 'attempt_reserved': False}
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
            if completed and code == 429:
                # Внешний заголовок превращается только в ограниченный серверный срок ожидания.
                retry_seconds = 60
                raw_retry = response.headers.get('Retry-After', '')
                try:
                    retry_seconds = max(1, min(300, int(raw_retry)))
                except (ValueError, TypeError):
                    pass
                usage['retry_after'] = (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)).isoformat()
            logger.warning('Сбой перевода обложки id=%s, type=%s, code=%s',
                           media_id, type(error).__name__, code if isinstance(code, int) else None)
            result, uncertain = {'status': 'error'}, not completed
        # Метаданные ревизии не меняют ключ кэша и не возвращают потраченные попытки.
        usage = {**usage, 'prompt_revision': PROMPT_REVISION,
                 'image_preprocessing': image_normalization, 'source_kind': source_kind,
                 'source_failures': [{'source_kind': kind, 'reason_code': reason} for kind, reason in failures]}
        saved = await database(store.finish, media_id, row['claim_token'], result, usage,
                               int((time.monotonic() - started) * 1000), uncertain)
        return {'item': item(media_id, {**saved, 'attempt_recorded': True}), 'attempt_reserved': True}
    finally:
        try:
            await client.aio.aclose()
            client.close()
        except Exception:  # noqa: BLE001 — наружу нельзя выпускать ошибки провайдера и реквизиты доступа.
            logger.warning('Не удалось закрыть транспорт перевода обложки')
