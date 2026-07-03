from __future__ import annotations

import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.models.schemas import ImportRequest, ImportResponse
from app.pipeline.normalize import parse_links
from app.services.import_service import _done_shortcodes, handle_import

router = APIRouter(tags=["import"])

# Rate-limit по IP: защита кошелька — публичный сервис работает на ключах владельца.
# In-memory: при нескольких инстансах API лимит действует на каждый инстанс отдельно — ок для нас.
RATE_MAX_IMPORTS = 5
RATE_WINDOW_SEC = 600
_rate_hits: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_WINDOW_SEC]
    if len(hits) >= RATE_MAX_IMPORTS:
        raise HTTPException(
            status_code=429,
            detail=f'Слишком много импортов подряд (лимит {RATE_MAX_IMPORTS} за '
                   f'{RATE_WINDOW_SEC // 60} минут). Подожди немного и попробуй снова.',
        )
    hits.append(now)
    _rate_hits[ip] = hits
    if len(_rate_hits) > 10_000:   # не даём словарю расти бесконечно
        _rate_hits.clear()


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
