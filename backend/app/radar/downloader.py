"""Скачивание видео рилса во временную папку (Радар, этап разбора).

Ссылка `video_url` — подписанный адрес Instagram CDN с ограниченным сроком жизни
(параметр `oe` в самой ссылке: unix-время истечения в hex). Протухшую или отклонённую
(403/404) ссылку освежаем ОДНИМ вызовом Apify-актора reel-scraper по прямой ссылке на
рилс — так же, как делает старый локальный Радар
(`../../../Радар/backend/services/apify_scraper.py:refresh_video_urls`), только на один
рилс за раз и через общий клиент с ротацией токенов (`app.pipeline.apify_client`).

yt-dlp не используется: на Vercel без cookies браузера он бесполезен (это уже
подтверждено на этапе 5 ТЗ проекта — "rate-limit reached or login required"), а cookies
для serverless-функции взять неоткуда.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.db import get_db
from app.pipeline.apify_client import run_actor_get_items
from app.radar import settings

logger = logging.getLogger(__name__)

# Запас перед истечением ссылки — как зафиксировано в ТЗ (10 минут).
_EXPIRY_MARGIN_SEC = 600

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_APIFY_TIMEOUT = httpx.Timeout(90.0, connect=15.0)


class DownloadError(Exception):
    """Видео не удалось скачать ни одним способом. Сообщение — человекочитаемое, на русском."""


def link_expired(url: str | None) -> bool:
    """True, если у ссылки истёк (или истекает меньше чем через 10 минут) срок жизни.

    Смотрим параметр `oe` в query-строке ссылки — unix-время истечения в hex, так
    подписывает свои CDN-ссылки Instagram. Ссылка без этого параметра или с
    нераспознаваемым значением считается живой: пусть решает сам запрос на скачивание.
    """
    if not url:
        return True
    try:
        query = parse_qs(urlparse(url).query)
        oe = query.get("oe", [None])[0]
        if not oe:
            return False
        expires_at = int(oe, 16)
        return time.time() >= (expires_at - _EXPIRY_MARGIN_SEC)
    except (ValueError, TypeError):
        return False


async def _download_httpx(url: str, dest: Path) -> tuple[bool, str | None]:
    """Стрим-скачивание по прямой ссылке на диск. Возвращает (успех, текст_ошибки_или_None)."""
    try:
        # Без скобок вокруг менеджеров: с ними синтаксис требует Python 3.10+, а проект
        # держит совместимость с 3.9 (см. CLAUDE.md).
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client, \
                client.stream("GET", url, headers=_HEADERS) as resp:
            if resp.status_code in (403, 404):
                return False, f"ссылка на видео недействительна (HTTP {resp.status_code})"
            resp.raise_for_status()
            # Блокирующая запись на диск — намеренно (не asyncio.to_thread на каждый чанк):
            # функция выполняется в одном serverless-запросе Vercel без конкурентных задач
            # в event loop, дробить запись по потокам здесь — накладные расходы без выгоды.
            with open(dest, "wb") as f:  # noqa: ASYNC230
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
        return True, None
    except httpx.HTTPStatusError as e:
        logger.warning("httpx: ошибка статуса при скачивании: %s", e)
        return False, f"ошибка HTTP при скачивании видео: {e.response.status_code}"
    except Exception as e:  # noqa: BLE001 — любая сетевая проблема здесь временная, не валим пайплайн
        logger.warning("httpx: скачивание не удалось: %s", e)
        return False, f"скачивание видео не удалось: {e}"


async def refresh_video_urls(reel_ids: list[str]) -> dict[str, str]:
    """Освежает `video_url` заданных рилсов ОДНИМ вызовом Apify reel-scraper.

    Опорный факт (подтверждён в старом Радаре): актор `apify~instagram-reel-scraper`
    принимает в поле `username` не только имена аккаунтов, но и прямые ссылки на рилсы —
    тогда `resultsLimit` игнорируется, а актор возвращает по одному свежему объекту на
    каждую переданную ссылку.

    Пишет в `radar_reels` только `video_url` (+ `updated_at`) — метрики
    views/likes/comments/ссылку на прогон собирает и пишет модуль сбора агента A
    (`radar/repo.py`), здесь их не трогаем, чтобы не затирать параллельную запись.

    Сбой вызова Apify (нет токенов, лимит, сетевая ошибка) не бросает исключение — просто
    ничего не обновляет; вызывающий код (`download_to`/`pipeline.refresh_expired_links`)
    сам решает, что делать дальше (там это ошибка скачивания, а не крах пайплайна).
    """
    if not reel_ids:
        return {}

    db = get_db()
    rows = (
        db.table("radar_reels").select("id,url").in_("id", reel_ids).execute()
    ).data or []
    links = [r["url"] for r in rows if r.get("url")]
    if not links:
        logger.warning("refresh_video_urls: для %s нет сохранённых ссылок на рилсы", reel_ids)
        return {}

    try:
        async with httpx.AsyncClient(timeout=_APIFY_TIMEOUT) as client:
            items = await run_actor_get_items(client, settings.REEL_ACTOR, {"username": links})
    except Exception as e:  # noqa: BLE001 — Apify недоступен/лимит исчерпан — не роняем разбор
        logger.warning("refresh_video_urls: вызов Apify не удался: %s", e)
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    result: dict[str, str] = {}
    for item in items:
        reel_id = item.get("shortCode") or item.get("id")
        video_url = item.get("videoUrl")
        if not reel_id or not video_url:
            continue
        try:
            db.table("radar_reels").update(
                {"video_url": video_url, "updated_at": now_iso}
            ).eq("id", reel_id).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("refresh_video_urls: не удалось записать video_url для %s: %s", reel_id, e)
            continue
        result[reel_id] = video_url

    logger.info("refresh_video_urls: обновлено %d из %d рилсов", len(result), len(reel_ids))
    return result


async def download_to(tmpdir: str | Path, reel: dict) -> Path:
    """Качает mp4 рилса во временную папку, возвращает путь к файлу.

    Порядок попыток (yt-dlp не используется — см. docstring модуля):
    httpx по текущей ссылке (если не протухла) → освежить ОДНУ ссылку через Apify и
    повторить httpx. Любая неудача на обоих шагах — `DownloadError` с человеческим текстом.
    """
    reel_id = reel["id"]
    dest = Path(tmpdir) / f"{reel_id}.mp4"

    video_url = reel.get("video_url")
    last_error: str | None = None

    if video_url and not link_expired(video_url):
        ok, err = await _download_httpx(video_url, dest)
        if ok:
            return dest
        last_error = err
        logger.info("Рилс %s: httpx по текущей ссылке не сработал (%s), обновляю ссылку", reel_id, err)
    else:
        last_error = "ссылка на видео истекла"

    fresh = await refresh_video_urls([reel_id])
    fresh_url = fresh.get(reel_id)
    if not fresh_url:
        raise DownloadError(last_error or "Не удалось обновить ссылку на видео через Apify")

    ok, err = await _download_httpx(fresh_url, dest)
    if ok:
        return dest

    raise DownloadError(err or "Скачивание не удалось и после обновления ссылки")
