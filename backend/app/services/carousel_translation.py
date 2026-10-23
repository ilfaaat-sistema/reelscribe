"""Перевод сохранённой подписи с общим кэшем, лимитом и арендой в БД."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from uuid import uuid4

from fastapi import HTTPException

from app.core.translation_db import get_translation_db
from app.pipeline.translate import (
    TranslationFailedError,
    needs_translation,
)

from app.services.carousel_caption_settings import get_settings
from app.services.carousel_caption_auth import CaptionAuthError, oidc_credentials
from app.services.carousel_caption_vertex import (
    CaptionVertexError, CaptionVertexAuthError, CaptionVertexQuotaError,
    CaptionVertexUnknown, caption_failure_reason, translate_caption,
)

logger = logging.getLogger(__name__)
MAX_SOURCE_LENGTH = 12_000
TABLE = 'carousel_caption_translations'
COLUMNS = 'translation,source_language'
_SAFE_FAILURE_REASONS = frozenset({
    'empty', 'error_marker', 'too_short', 'no_cyrillic', 'unchanged', 'quota_exceeded',
    'auth', 'schema', 'blocked', 'finish', 'provider', 'unknown',
})


class TranslationError(Exception):
    def __init__(self, status, code, detail, retry_after=None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


def source_hash(source):
    return hashlib.sha256((source or '').encode('utf-8')).hexdigest()


def _language(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'[A-Za-z-]{2,12}', value) else None


def _value(source, state, translation=None, language=None):
    return {'source_hash': source_hash(source), 'state': state,
            'translation': translation, 'source_language': _language(language)}


def _cached(db, media_id, digest):
    rows = (db.table(TABLE).select(COLUMNS).eq('instagram_media_id', media_id)
            .eq('source_hash', digest).limit(1).execute().data or [])
    return rows[0] if rows else None


def _rpc(db, name, args):
    value = db.rpc(name, args).execute().data
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError('Неверный ответ хранилища перевода')
    return value


def _unavailable():
    return TranslationError(503, 'translation_unavailable', 'Перевод временно недоступен. Попробуйте позже.')


def _log_translation_failure(exc):
    # Причина может содержать произвольный текст; в журнал попадает только известный код.
    reason = exc.reason if isinstance(exc, (TranslationFailedError, CaptionVertexError)) else None
    safe_reason = reason if isinstance(reason, str) and reason in _SAFE_FAILURE_REASONS else 'unknown'
    logger.warning('Ошибка перевода карусели: %s reason=%s', type(exc).__name__, safe_reason)


async def detail_translation(item):
    """Ошибка нового кэша не мешает читать существующую карточку."""
    source = item.get('caption') or ''
    if not needs_translation(source):
        return _value(source, 'not_needed')
    if len(source) > MAX_SOURCE_LENGTH:
        return _value(source, 'unavailable')
    try:
        db = get_translation_db()
        row = await asyncio.to_thread(_cached, db, str(item['id']), source_hash(source))
    except Exception as exc:
        logger.info('Кэш перевода карусели недоступен: %s', type(exc).__name__)
        return _value(source, 'unavailable')
    if row and caption_failure_reason(source, row.get('translation')) is None:
        return _value(source, 'ready', row['translation'], row.get('source_language'))
    try:
        available = get_settings().enabled
    except Exception:
        available = False
    return _value(source, 'available' if available else 'unavailable')


async def _source(media_id, period):
    # Импорт локальный: API деталей использует только обогащение, без цикла модулей.
    from app.api.carousels import resolve_detail

    try:
        return await resolve_detail(media_id, period)
    except HTTPException as exc:
        code = 'carousel_source_unavailable' if exc.status_code == 503 else 'carousel_not_found'
        raise TranslationError(exc.status_code, code, exc.detail) from None
    except Exception as exc:
        logger.warning('Источник карусели недоступен: %s', type(exc).__name__)
        raise TranslationError(503, 'carousel_source_unavailable',
                               'Не удалось прочитать публикацию. Попробуйте позже.') from None


def _finish(db, media_id, digest, token, *, translation=None, language=None, outcome='failed'):
    return _rpc(db, 'finish_carousel_caption_translation', {
        'p_id': media_id, 'p_source_hash': digest, 'p_attempt_id': token,
        'p_translation': translation, 'p_source_language': _language(language), 'p_outcome': outcome,
    })


async def _end_failure(db, media_id, digest, token, outcome='failed'):
    try:
        await asyncio.to_thread(_finish, db, media_id, digest, token, outcome=outcome)
    except Exception as exc:
        # Не освобождаем аренду в памяти: БД сохраняет ограничение до срока истечения.
        logger.warning('Не удалось закрыть аренду перевода: %s', type(exc).__name__)


async def translate(media_id, period, expected_hash, *, oidc_token=None):
    if not re.fullmatch(r'[0-9]{1,40}', media_id):
        raise TranslationError(422, 'invalid_request', 'Некорректный ID карусели')
    item = await _source(media_id, period)
    source = item.get('caption') or ''
    digest = source_hash(source)
    if digest != expected_hash:
        raise TranslationError(409, 'source_changed', 'Текст публикации изменился. Обновите карточку.')
    if not source.strip():
        raise TranslationError(409, 'source_missing', 'Исходный текст отсутствует.')
    if len(source) > MAX_SOURCE_LENGTH:
        raise TranslationError(413, 'source_too_long', 'Текст слишком длинный для перевода.')
    if not needs_translation(source):
        raise TranslationError(409, 'translation_not_needed', 'Этот текст уже на русском или не требует перевода.')

    try:
        db = get_translation_db()
        row = await asyncio.to_thread(_cached, db, media_id, digest)
        if row and caption_failure_reason(source, row.get('translation')) is None:
            return {'id': media_id, 'caption_translation': _value(
                source, 'ready', row['translation'], row.get('source_language')), 'cached': True}
    except Exception as exc:
        logger.warning('Кэш перевода карусели недоступен: %s', type(exc).__name__)
        raise _unavailable() from None

    try:
        config = get_settings()
        if not config.enabled:
            raise CaptionAuthError('Перевод выключен')
        credentials = await asyncio.to_thread(oidc_credentials, oidc_token, config=config)
    except Exception:
        # Включая STS/IAM: до reserve, без попытки модели и сырых исключений.
        raise _unavailable() from None

    try:
        if row:
            # Сравнение текста защищает от удаления исправленного конкурентом кэша.
            await asyncio.to_thread(lambda: db.table(TABLE).delete()
                                    .eq('instagram_media_id', media_id).eq('source_hash', digest)
                                    .eq('translation', row['translation']).execute())
        token = str(uuid4())
        reserved = await asyncio.to_thread(_rpc, db, 'reserve_carousel_caption_translation', {
            'p_id': media_id, 'p_source_hash': digest, 'p_attempt_id': token,
        })
    except Exception as exc:
        logger.warning('Не удалось зарезервировать перевод: %s', type(exc).__name__)
        raise _unavailable() from None

    state = reserved.get('state')
    if state == 'ready':
        if caption_failure_reason(source, reserved.get('translation')) is not None:
            raise _unavailable()
        return {'id': media_id, 'caption_translation': _value(
            source, 'ready', reserved['translation'], reserved.get('source_language')), 'cached': True}
    if state == 'in_progress':
        raise TranslationError(409, 'translation_in_progress', 'Перевод уже выполняется. Попробуйте чуть позже.', 5)
    if state == 'limited':
        retry = reserved.get('retry_after')
        retry = retry if isinstance(retry, int) and 1 <= retry <= 3600 else 60
        raise TranslationError(429, 'translation_rate_limited', 'Лимит переводов достигнут. Попробуйте позже.', retry)
    if state != 'reserved' or reserved.get('attempt_id') != token:
        raise _unavailable()

    try:
        translated, language = await translate_caption(source, credentials=credentials, config=config)
        reason = caption_failure_reason(source, translated)
        if reason is not None:
            raise TranslationFailedError(reason)
    except CaptionVertexQuotaError as exc:
        _log_translation_failure(exc)
        await _end_failure(db, media_id, digest, token, 'quota')
        raise TranslationError(429, 'translation_quota_exceeded', 'Квота переводчика исчерпана. Попробуйте позже.') from None
    except CaptionVertexAuthError as exc:
        _log_translation_failure(exc)
        await _end_failure(db, media_id, digest, token)
        raise _unavailable() from None
    except CaptionVertexUnknown as exc:
        _log_translation_failure(exc)
        # Потраченный бюджет неизвестен: сохраняем lease до штатных 15 минут.
        raise TranslationError(502, 'translation_failed', 'Перевод не удался. Попробуйте позже.') from None
    except Exception as exc:
        _log_translation_failure(exc)
        await _end_failure(db, media_id, digest, token)
        raise TranslationError(502, 'translation_failed', 'Перевод не удался. Попробуйте позже.') from None

    try:
        current = await _source(media_id, period)
    except TranslationError:
        await _end_failure(db, media_id, digest, token)
        raise
    if source_hash(current.get('caption') or '') != digest:
        await _end_failure(db, media_id, digest, token, 'source_changed')
        raise TranslationError(409, 'source_changed', 'Текст публикации изменился. Обновите карточку.')
    try:
        saved = await asyncio.to_thread(_finish, db, media_id, digest, token,
                                        translation=translated, language=language, outcome='ready')
    except Exception as exc:
        logger.warning('Не удалось сохранить перевод карусели: %s', type(exc).__name__)
        raise _unavailable() from None
    if saved.get('state') != 'ready':
        raise TranslationError(409, 'translation_in_progress', 'Срок ожидания перевода истёк. Повторите запрос.', 5)
    if caption_failure_reason(source, saved.get('translation')) is not None:
        raise _unavailable()
    return {'id': media_id, 'caption_translation': _value(
        source, 'ready', saved['translation'], saved.get('source_language')), 'cached': False}
