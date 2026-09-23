from __future__ import annotations

import re
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response

router = APIRouter(tags=["thumb"])

# shortcode из URL instagram.com/(reel|p|tv)/<shortcode> — только этот алфавит встречается
# в реальных ссылках IG. Эндпоинт публичный (auth в проекте нет — осознанное решение), поэтому
# без строгой валидации сюда можно подставить произвольный путь в запросе к instagram.com.
_SHORTCODE_RE = re.compile(r'^[A-Za-z0-9_-]{5,30}$')

# Публичный путь обложки поста/рилса/карусели — не требует токена и авторизации.
# /p/ универсален для всех трёх типов (тот же приём — в ReelDrawer.jsx для embed-плеера).
_INSTAGRAM_MEDIA_URL = 'https://www.instagram.com/p/{shortcode}/media/'

_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

# Vercel Serverless Response ограничен 4.5 МБ — реальные обложки (t/m/l ~5-40 КБ по замеру)
# на два порядка меньше, но подстраховываемся: не проксируем "картинку" мегабайтного размера
# (например, если Instagram вдруг отдаст HTML-страницу под image/* content-type).
_MAX_BYTES = 2 * 1024 * 1024

# Кэш на CDN Vercel (edge), не в браузере: s-maxage — сколько edge отдаёт закэшированный ответ
# без похода к нам; stale-while-revalidate — сколько ещё разрешено отдавать протухшую копию,
# пока в фоне идёт обновление (пользователь никогда не ждёт живого похода в Instagram).
# Сутки основного кэша + неделя "протухшего на подхвате" — обложка рилса не меняется, столько
# кэшировать безопасно, а хвост stale-while-revalidate сглаживает случай, когда Instagram
# на пару часов недоступен для наших запросов.
_CACHE_CONTROL = 'public, s-maxage=86400, stale-while-revalidate=604800'


@router.get("/thumb/{shortcode}")
async def get_thumb(
    shortcode: str = Path(...),
    size: Literal["t", "m", "l"] = Query("m"),
) -> Response:
    """Посредник к публичной обложке поста/рилса Instagram.

    Instagram блокирует загрузку картинки напрямую в <img src> с чужого домена (проверено
    в браузере — см. ТЗ модуля), но отдаёт её обычному серверному запросу. Отдаём наружу
    саму картинку с нашего домена и с длинным CDN-кэшем, чтобы не дёргать Instagram на
    каждый рендер таблицы.
    """
    if not _SHORTCODE_RE.match(shortcode):
        raise HTTPException(400, "некорректный shortcode")

    url = _INSTAGRAM_MEDIA_URL.format(shortcode=shortcode)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            follow_redirects=True,
            headers={'User-Agent': _UA},
        ) as client:
            resp = await client.get(url, params={'size': size})
    except httpx.HTTPError:
        # Instagram недоступен/оборвал соединение — 404, чтобы фронт спокойно показал
        # прежний серый градиент вместо сломанной иконки.
        raise HTTPException(404, "обложка недоступна")

    content_type = resp.headers.get('content-type', '')
    if resp.status_code != 200 or not content_type.startswith('image/'):
        raise HTTPException(404, "обложка недоступна")

    if len(resp.content) > _MAX_BYTES:
        raise HTTPException(404, "обложка слишком большая")

    return Response(
        content=resp.content,
        media_type=content_type,
        headers={'Cache-Control': _CACHE_CONTROL},
    )
