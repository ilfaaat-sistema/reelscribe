from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """Лимит запросов RapidAPI/StarAPI исчерпан (HTTP 429)."""

# StarAPI (RapidAPI) — зеркало приватного API Instagram. Достаёт медиа по shortcode.
_MEDIA_ENDPOINT = "/instagram/media/get_info_by_shortcode"
_PROFILE_ENDPOINT = "/instagram/user/get_web_profile_info"

# Кэш подписчиков по username: рилсы одного автора → один вызов профиля (экономим квоту).
_followers_cache: dict[str, int | None] = {}
_followers_lock: asyncio.Lock | None = None

# Ключи на «остывании» после HTTP 429: {key: monotonic-время, когда ключ снова можно пробовать}.
# Не выключаем ключ навсегда — иначе один временный rate-limit убивал бы ключ на весь запуск воркера.
_cooldown: dict[str, float] = {}


def _available_keys() -> list[str]:
    now = time.monotonic()
    return [k for k in settings.rapidapi_key_list if _cooldown.get(k, 0.0) <= now]


def _cool_down(key: str) -> None:
    _cooldown[key] = time.monotonic() + settings.starapi_key_cooldown_min * 60


_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    # Ленивая инициализация под ТЕКУЩИЙ event loop (Python 3.9): пересоздаём
    # и при смене loop после рестарта, иначе «attached to a different loop».
    global _followers_lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _followers_lock is None or _lock_loop is not loop:
        _followers_lock = asyncio.Lock()
        _lock_loop = loop
    return _followers_lock


def _shortcode_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _headers(key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-rapidapi-host": settings.starapi_host,
        "x-rapidapi-key": key,
    }


async def _request(client: httpx.AsyncClient, endpoint: str, payload: dict) -> httpx.Response:
    """POST к StarAPI с ротацией ключей: при 429 ключ уходит в cooldown и берём следующий."""
    available = _available_keys()
    if not available:
        raise QuotaExceededError("StarAPI: все ключи на cooldown после 429")
    for key in available:
        resp = await client.post(
            f"https://{settings.starapi_host}{endpoint}",
            headers=_headers(key),
            json=payload,
        )
        if resp.status_code == 429:
            _cool_down(key)
            logger.warning(
                "StarAPI: ключ …%s — 429, cooldown %dмин",
                key[-6:], settings.starapi_key_cooldown_min,
            )
            continue
        return resp
    raise QuotaExceededError("StarAPI: все ключи на cooldown после 429")


def _ts_to_yyyymmdd(taken_at: int | None) -> str | None:
    if not taken_at:
        return None
    try:
        return datetime.fromtimestamp(int(taken_at), tz=timezone.utc).strftime("%Y%m%d")
    except Exception:
        return None


async def _get_followers(client: httpx.AsyncClient, username: str | None) -> int | None:
    """Подписчики автора через профиль-эндпоинт, с кэшем по username."""
    if not username:
        return None
    async with _get_lock():
        if username in _followers_cache:
            return _followers_cache[username]
    try:
        resp = await _request(client, _PROFILE_ENDPOINT, {"username": username})
        resp.raise_for_status()
        user = (
            resp.json().get("response", {}).get("body", {}).get("data", {}).get("user", {})
        )
        count = (user.get("edge_followed_by") or {}).get("count")
    except QuotaExceededError:
        # Квота кончилась только на профиле — не валим рилс, просто без подписчиков.
        count = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("StarAPI: профиль %s не получен (%s)", username, exc)
        count = None
    async with _get_lock():
        _followers_cache[username] = count
    return count


async def fetch_via_starapi(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Скачивание аудио + метаданные рилса через StarAPI (RapidAPI)."""
    if not settings.rapidapi_key_list:
        raise RuntimeError("RAPIDAPI_KEY(S) не задан — StarAPI недоступен")

    shortcode = _shortcode_from_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as client:
        resp = await _request(client, _MEDIA_ENDPOINT, {"shortcode": shortcode})
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("body", {}).get("items") or []
        if not items:
            raise RuntimeError(f"StarAPI: нет данных по {shortcode}")
        item = items[0]

        video_versions = item.get("video_versions") or []
        if not video_versions:
            raise RuntimeError(f"StarAPI: {shortcode} без видеодорожки (медиа-тип {item.get('media_type')})")
        video_url = video_versions[0]["url"]

        author = (item.get("user") or {}).get("username")
        followers = await _get_followers(client, author)

        # скачиваем CDN-видео напрямую (этот шаг авторизации не требует)
        video_path = dest_dir / f"{shortcode}_star.mp4"
        wav_path = dest_dir / f"{shortcode}_star.wav"
        async with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with video_path.open("wb") as f:
                async for chunk in stream.aiter_bytes(8192):
                    f.write(chunk)

    # извлекаем аудио через ffmpeg
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = await asyncio.to_thread(
        subprocess.run,
        [ffmpeg_exe, "-y", "-i", str(video_path),
         "-ar", "16000", "-ac", "1", str(wav_path)],
        capture_output=True, text=True,
    )
    video_path.unlink(missing_ok=True)
    if result.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"ffmpeg не извлёк аудио: {result.stderr[:300]}")

    info: dict[str, Any] = {
        "id": shortcode,
        "uploader_id": author,
        "channel_follower_count": followers,
        "view_count": item.get("play_count") or item.get("ig_play_count"),
        "like_count": item.get("like_count"),
        "comment_count": item.get("comment_count"),
        "description": (item.get("caption") or {}).get("text"),
        "upload_date": _ts_to_yyyymmdd(item.get("taken_at")),
    }
    logger.info(
        "StarAPI ✓ %s (views=%s likes=%s followers=%s)",
        shortcode, info["view_count"], info["like_count"], followers,
    )
    return wav_path, info
