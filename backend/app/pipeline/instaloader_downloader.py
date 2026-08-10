"""Бесплатный источник данных Instagram на базе instaloader (GitHub-парсер).

Две роли, обе снимают зависимость от платной квоты StarAPI:
  1. get_followers_via_instaloader(username) — число подписчиков автора (кэш по username).
  2. fetch_via_instaloader(url, dest_dir)     — аудио по shortcode (фолбэк, если Apify-профиль промахнулся).

instaloader синхронный → все сетевые вызовы уводим в asyncio.to_thread. Кэши/локи/семафор —
loop-aware (Python 3.9: примитивы asyncio привязаны к loop, пересоздаём при смене loop после рестарта).

Без логина Instagram жёстко режет анонимные запросы (401/429 «Please wait a few minutes»). Такие сбои
трактуем мягко: followers → None, аудио → InstaloaderUnavailable (каскад идёт дальше, партия не падает).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

import httpx
import instaloader
from instaloader.exceptions import (
    ConnectionException,
    InstaloaderException,
    LoginRequiredException,
    PrivateProfileNotFollowedException,
    ProfileNotExistsException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)

from app.core.config import settings
from app.pipeline.apify_profile_downloader import NoAudioError
from app.pipeline.media import extract_wav_16k_mono

logger = logging.getLogger(__name__)


class InstaloaderUnavailable(Exception):
    """Рилс не достать через instaloader (приватный/удалён/rate-limit/login-required) — мягкий фолбэк дальше."""


# Ленивый синглтон Instaloader (при заданной сессии — залогиненный, лимиты выше).
_loader: instaloader.Instaloader | None = None

# Кэш подписчиков по username (как в starapi_downloader) + loop-aware лок и семафор.
_followers_cache: dict[str, int | None] = {}
_followers_lock: asyncio.Lock | None = None
_semaphore: asyncio.Semaphore | None = None
_loop: asyncio.AbstractEventLoop | None = None

# Анти-бан: после 429/rate-limit «замолкаем» до этого времени (monotonic), чтобы не спалить аккаунт.
_paused_until: float = 0.0


def _is_paused() -> bool:
    return time.monotonic() < _paused_until


def _pause() -> None:
    global _paused_until
    _paused_until = time.monotonic() + settings.instaloader_cooldown_min * 60
    logger.warning("instaloader: rate-limit — пауза на %d мин", settings.instaloader_cooldown_min)


async def _human_delay() -> None:
    await asyncio.sleep(random.uniform(settings.instaloader_min_delay_sec, settings.instaloader_max_delay_sec))


def _loader_instance() -> instaloader.Instaloader:
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            max_connection_attempts=1,   # не долбим IG ретраями — при сбое мягко уходим в фолбэк
        )
        if settings.instaloader_session_file and settings.instaloader_user:
            try:
                _loader.load_session_from_file(
                    settings.instaloader_user, settings.instaloader_session_file,
                )
                logger.info("instaloader: сессия @%s загружена", settings.instaloader_user)
            except Exception as exc:  # noqa: BLE001
                logger.warning("instaloader: сессию загрузить не удалось (%s) — работаю анонимно", exc)
    return _loader


def _sync() -> tuple[asyncio.Lock, asyncio.Semaphore]:
    """Loop-aware лок и семафор=1 (один поток — анти-бан; пересоздаём при смене loop, Python 3.9)."""
    global _followers_lock, _semaphore, _loop
    loop = asyncio.get_running_loop()
    if _followers_lock is None or _semaphore is None or _loop is not loop:
        _followers_lock = asyncio.Lock()
        _semaphore = asyncio.Semaphore(1)   # строго один запрос за раз
        _loop = loop
    return _followers_lock, _semaphore


def _shortcode_from_url(url: str) -> str:
    return url.split("?")[0].rstrip("/").split("/")[-1]


async def get_followers_via_instaloader(username: str | None) -> int | None:
    """Подписчики автора через instaloader, с кэшем по username. Любой сбой → None (не валим рилс)."""
    if not username or not settings.instaloader_enabled:
        return None
    lock, sem = _sync()
    async with lock:
        if username in _followers_cache:
            return _followers_cache[username]
    if _is_paused():
        return None   # на rate-limit-паузе не трогаем IG

    def _fetch() -> int | None:
        profile = instaloader.Profile.from_username(_loader_instance().context, username)
        return profile.followers

    count: int | None = None
    try:
        async with sem:
            count = await asyncio.to_thread(_fetch)
            await _human_delay()   # 12-30с рандомно — имитация человека, анти-бан
    except TooManyRequestsException:
        _pause()
    except ProfileNotExistsException:
        logger.info("instaloader: профиль @%s не существует", username)
    except (ConnectionException, LoginRequiredException) as exc:
        logger.warning("instaloader: @%s недоступен (%s) — без подписчиков", username, exc)
    except InstaloaderException as exc:
        logger.warning("instaloader: @%s ошибка (%s)", username, exc)

    async with lock:
        _followers_cache[username] = count
    return count


async def fetch_via_instaloader(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Аудио по shortcode через instaloader. Подписчиков тут НЕ тянем — их дотянет воркер (кэш по автору)."""
    if not settings.instaloader_enabled:
        raise InstaloaderUnavailable("instaloader выключен")

    shortcode = _shortcode_from_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _lock, sem = _sync()
    if _is_paused():
        raise InstaloaderUnavailable(f"{shortcode}: instaloader на rate-limit-паузе")

    def _get_post() -> instaloader.Post:
        return instaloader.Post.from_shortcode(_loader_instance().context, shortcode)

    try:
        async with sem:
            post = await asyncio.to_thread(_get_post)
            await _human_delay()   # анти-бан: пауза после сетевого запроса к IG
    except TooManyRequestsException as exc:
        _pause()
        raise InstaloaderUnavailable(f"{shortcode}: rate-limit — пауза") from exc
    except QueryReturnedNotFoundException as exc:
        raise InstaloaderUnavailable(f"{shortcode}: пост не найден/удалён") from exc
    except (LoginRequiredException, PrivateProfileNotFollowedException) as exc:
        raise InstaloaderUnavailable(f"{shortcode}: нужен логин/приватный аккаунт") from exc
    except ConnectionException as exc:
        raise InstaloaderUnavailable(f"{shortcode}: rate-limit/сеть ({exc})") from exc
    except InstaloaderException as exc:
        raise InstaloaderUnavailable(f"{shortcode}: {exc}") from exc

    if not post.is_video:
        raise NoAudioError(f"{shortcode}: фото/карусель — нет аудио")
    video_url = post.video_url
    if not video_url:
        raise InstaloaderUnavailable(f"{shortcode}: видео без video_url")

    author = post.owner_username

    video_path = dest_dir / f"{shortcode}_insta.mp4"
    wav_path = dest_dir / f"{shortcode}_insta.wav"
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with video_path.open("wb") as f:
                async for chunk in stream.aiter_bytes(8192):
                    f.write(chunk)

    await extract_wav_16k_mono(video_path, wav_path)

    info: dict[str, Any] = {
        "id": shortcode,
        "uploader_id": author,
        "channel_follower_count": None,   # подписчиков дотянет воркер через get_followers_via_instaloader
        "view_count": post.video_view_count,
        "like_count": post.likes,
        "comment_count": post.comments,
        "description": post.caption,
        "upload_date": post.date_utc.strftime("%Y%m%d") if post.date_utc else None,
    }
    logger.info(
        "instaloader ✓ %s (@%s views=%s likes=%s)",
        shortcode, author, info["view_count"], info["like_count"],
    )
    return wav_path, info
