"""Перевод сохранённых подписи и расшифровки по явному запросу пользователя."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from threading import Lock
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.db import get_db
from app.pipeline.translate import (
    TranslationFailedError,
    TranslationQuotaExceededError,
    needs_translation,
    translate_to_ru_detailed,
    translation_failure_reason,
)

router = APIRouter(tags=["reels"])
logger = logging.getLogger(__name__)

# Предохранители для публичного endpoint: не переводим большие тексты и ограничиваем
# новые платные обращения в пределах процесса. Для общего лимита инстансов нужен
# отдельный общий счётчик; в рамках этой версии схема БД не меняется.
MAX_SOURCE_LENGTH = 12_000
MAX_IN_FLIGHT = 2
MAX_ATTEMPTS_PER_HOUR = 12
_guard = Lock()
_in_flight: set[tuple[str, str]] = set()
_attempts: deque[float] = deque()


class TranslationRequest(BaseModel):
    field: Literal['caption', 'transcript']


def _reserve(key: tuple[str, str]) -> None:
    now = time.monotonic()
    with _guard:
        while _attempts and _attempts[0] <= now - 3600:
            _attempts.popleft()
        if key in _in_flight:
            raise HTTPException(409, "перевод уже выполняется")
        if len(_in_flight) >= MAX_IN_FLIGHT or len(_attempts) >= MAX_ATTEMPTS_PER_HOUR:
            raise HTTPException(429, "лимит переводов достигнут, попробуйте позже")
        _in_flight.add(key)
        _attempts.append(now)


def _release(key: tuple[str, str]) -> None:
    with _guard:
        _in_flight.discard(key)


@router.post('/reels/{reel_id}/translate')
async def translate_reel(reel_id: UUID, body: TranslationRequest) -> dict:
    db = get_db()
    reel_id_str = str(reel_id)
    if body.field == 'caption':
        rows = db.table('reels').select('caption, caption_ru, caption_lang').eq('id', reel_id_str).execute().data
        if not rows:
            raise HTTPException(404, "рилс не найден")
        row = rows[0]
        source, cached = row.get('caption'), row.get('caption_ru')
    else:
        exists = db.table('reels').select('id').eq('id', reel_id_str).execute().data
        if not exists:
            raise HTTPException(404, "рилс не найден")
        rows = db.table('transcripts').select('text, text_ru, language').eq('reel_id', reel_id_str).execute().data
        row = rows[0] if rows else {}
        source, cached = row.get('text'), row.get('text_ru')

    if not source or not source.strip():
        raise HTTPException(409, "исходный текст отсутствует")
    if cached and translation_failure_reason(source, cached) is None:
        result = {'field': body.field, 'translation': cached}
        if body.field == 'caption':
            result['caption_lang'] = row.get('caption_lang')
        return result
    if len(source) > MAX_SOURCE_LENGTH:
        raise HTTPException(413, "текст слишком длинный для перевода")
    if not needs_translation(source):
        raise HTTPException(409, "исходный текст уже на русском или не требует перевода")

    key = (reel_id_str, body.field)
    _reserve(key)
    try:
        try:
            translated, language = await asyncio.to_thread(translate_to_ru_detailed, source)
            reason = translation_failure_reason(source, translated)
            if reason is not None:
                raise TranslationFailedError(reason)
        except TranslationQuotaExceededError:
            raise HTTPException(429, "квота переводчика исчерпана, попробуйте позже") from None
        except TranslationFailedError as exc:
            logger.warning('Перевод %s %s не прошёл проверку: %s', body.field, reel_id_str, exc.reason)
            raise HTTPException(502, "перевод не удался, попробуйте позже") from None
        except Exception as exc:
            logger.warning('Ошибка переводчика для %s %s: %s', body.field, reel_id_str, type(exc).__name__)
            raise HTTPException(502, "переводчик временно недоступен") from None

        if body.field == 'caption':
            update = {'caption_ru': translated}
            if language:
                update['caption_lang'] = language
            updated = db.table('reels').update(update).eq('id', reel_id_str).eq('caption', source).select('caption_ru').execute().data
            if not updated:
                raise HTTPException(409, "исходный текст изменился, обновите рилс")
            return {'field': body.field, 'translation': translated, 'caption_lang': language or row.get('caption_lang')}
        updated = db.table('transcripts').update({'text_ru': translated}).eq('reel_id', reel_id_str).eq('text', source).select('text_ru').execute().data
        if not updated:
            raise HTTPException(409, "исходный текст изменился, обновите рилс")
        return {'field': body.field, 'translation': translated}
    finally:
        _release(key)
