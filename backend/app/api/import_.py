from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.core.db import get_db
from app.models.schemas import ImportRequest, ImportResponse
from app.pipeline.normalize import parse_links
from app.services.import_service import _done_shortcodes, handle_import

logger = logging.getLogger(__name__)
router = APIRouter(tags=["import"])

# Rate-limit по IP: защита кошелька — публичный сервис работает на ключах владельца.
# Состояние живёт в таблице rate_limits, а НЕ в памяти процесса: API работает на serverless
# (Vercel), где каждый вызов может попасть в свежий инстанс, и словарь в памяти защищал бы
# ровно ничего.
RATE_MAX_IMPORTS = 5
RATE_WINDOW_SEC = 600


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def _check_rate_limit(ip: str) -> None:
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=RATE_WINDOW_SEC)
    ).isoformat()
    db = get_db()
    try:
        # Уборка старых отметок — таблица крошечная, чистим на каждом импорте.
        db.table('rate_limits').delete().lt('created_at', cutoff).execute()
        recent = (
            db.table('rate_limits').select('id')
            .eq('ip', ip).gte('created_at', cutoff)
            .execute().data
        )
    except Exception as exc:  # noqa: BLE001
        # Пропускаем: сам импорт всё равно упирается в ту же базу и упадёт следом, если она
        # недоступна. Это же даёт безопасную раскатку — код можно задеплоить до миграции.
        logger.warning('Rate-limit не проверен (%s) — пропускаю импорт', exc)
        return

    if len(recent) >= RATE_MAX_IMPORTS:
        raise HTTPException(
            status_code=429,
            detail=f'Слишком много импортов подряд (лимит {RATE_MAX_IMPORTS} за '
                   f'{RATE_WINDOW_SEC // 60} минут). Подожди немного и попробуй снова.',
        )
    db.table('rate_limits').insert({'ip': ip}).execute()


@router.post("/import", response_model=ImportResponse, status_code=status.HTTP_202_ACCEPTED)
async def import_links(
    body: ImportRequest, background_tasks: BackgroundTasks, request: Request,
) -> ImportResponse:
    if not body.links_text.strip():
        raise HTTPException(status_code=400, detail="links_text пустой")
    _check_rate_limit(_client_ip(request))
    return await handle_import(body, background_tasks=background_tasks)


@router.post("/import/preview")
async def import_preview(body: ImportRequest) -> dict:
    """Сколько из вставленных ссылок уже распознаны ранее (без создания сессии)."""
    parsed = parse_links(body.links_text)
    unique = list({p.shortcode: p for p in parsed}.values())
    reels = sum(1 for p in unique if p.type == 'reel')
    done = _done_shortcodes([p.shortcode for p in unique])
    return {
        'total': len(unique),
        'reels': reels,
        'posts': len(unique) - reels,
        'already_done': len(done),
        'new': len(unique) - len(done),
    }
