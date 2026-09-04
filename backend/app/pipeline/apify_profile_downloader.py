from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.pipeline.apify_client import run_actor_get_items
from app.pipeline.media import extract_wav_16k_mono
from app.pipeline.starapi_downloader import _ts_to_yyyymmdd

logger = logging.getLogger(__name__)

# Короткий UA: даёт лёгкую embed-страницу с автором. Длинный (Chrome/…) отдаёт тяжёлый app-shell без него.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# username из embed-страницы: ссылка вида instagram.com/<username>/?utm_source=ig_embed
_USERNAME_RE = re.compile(r'class="Username"[^>]*?instagram\.com/([A-Za-z0-9_.]{1,40})/\?utm_source=ig_embed')
_USERNAME_RE2 = re.compile(r'data-ios-link="user\?username=([A-Za-z0-9_.]{1,40})')

# Кэши (ленивые локи под текущий event loop, как в starapi_downloader — важно на Python 3.9).
_username_cache: dict[str, str | None] = {}
_profile_cache: dict[str, dict[str, dict]] = {}
_profile_locks: dict[str, asyncio.Lock] = {}
_locks_guard: asyncio.Lock | None = None


class ProfileMissError(Exception):
    """Рилс не получить через профильный актор (нет username / нет в свежей выдаче)."""


class NoAudioError(Exception):
    """Медиа точно без видеодорожки (фото/карусель) — расшифровывать нечего, терминально.

    info — метрики поста (просмотры/лайки/автор), если источник их отдал: у фото аудио нет,
    но статистику сохранить надо. Воркер запишет её в reels, несмотря на no_audio.
    """

    def __init__(self, message: str, info: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.info = info


def _shortcode_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


_guard_loop: asyncio.AbstractEventLoop | None = None


def _guard() -> asyncio.Lock:
    # Пересоздаём guard и пер-юзерные локи при смене event loop (рестарт после
    # краха на Python 3.9) — старые локи привязаны к мёртвому loop.
    global _locks_guard, _guard_loop
    loop = asyncio.get_running_loop()
    if _locks_guard is None or _guard_loop is not loop:
        _locks_guard = asyncio.Lock()
        _profile_locks.clear()
        _guard_loop = loop
    return _locks_guard


async def _profile_lock(username: str) -> asyncio.Lock:
    async with _guard():
        lock = _profile_locks.get(username)
        if lock is None:
            lock = asyncio.Lock()
            _profile_locks[username] = lock
        return lock


async def _resolve_username(client: httpx.AsyncClient, shortcode: str, is_reel: bool) -> str | None:
    """username автора из публичной embed-страницы (бесплатно, без логина/квоты)."""
    if shortcode in _username_cache:
        return _username_cache[shortcode]
    kind = "reel" if is_reel else "p"
    url = f"https://www.instagram.com/{kind}/{shortcode}/embed/captioned/"
    username: str | None = None
    try:
        resp = await client.get(url, headers={"User-Agent": _UA})
        if resp.status_code == 200:
            html = resp.text
            m = _USERNAME_RE.search(html) or _USERNAME_RE2.search(html)
            if m:
                username = m.group(1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed username %s: %s", shortcode, exc)
    _username_cache[shortcode] = username
    return username


async def _fetch_profile(client: httpx.AsyncClient, username: str) -> dict[str, dict]:
    """Один вызов дешёвого профильного актора Apify → map code→item. Кэш + лок по username."""
    lock = await _profile_lock(username)
    async with lock:
        if username in _profile_cache:
            return _profile_cache[username]
        # resultsLimit — для совместимости со старой версией актора, postsPerProfile — текущий
        # ключ схемы (иначе актор берёт дефолтные 24 поста и промахивается по старым рилсам).
        items = await run_actor_get_items(
            client, settings.apify_profile_actor,
            {
                "usernames": [username],
                "resultsLimit": settings.apify_profile_limit,
                "postsPerProfile": settings.apify_profile_limit,
            },
        )
        by_code = {it.get("code") or it.get("shortCode"): it for it in items if isinstance(it, dict)}
        by_code.pop(None, None)
        _profile_cache[username] = by_code
        logger.info("Apify-профиль %s: %d постов в кэш", username, len(by_code))
        return by_code


async def fetch_via_apify_profile(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Дешёвый путь: embed→username→профильный актор→матч shortcode. Подписчиков дотягивает воркер."""
    if not settings.apify_token_list:
        raise ProfileMissError("APIFY_API_TOKEN не задан")

    shortcode = _shortcode_from_url(url)
    is_reel = "/reel/" in url
    dest_dir.mkdir(parents=True, exist_ok=True)

    # таймаут ~90с (актор run-sync на глубине 120 может быть небыстрым), но не 300с —
    # зависшая загрузка/скрейп падает за ~90с, а не морозит воркер на 5 минут
    timeout = httpx.Timeout(90.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        username = await _resolve_username(client, shortcode, is_reel)
        if not username:
            raise ProfileMissError(f"username не определён для {shortcode}")

        posts = await _fetch_profile(client, username)
        item = posts.get(shortcode)
        if item is None:
            raise ProfileMissError(f"{shortcode} нет в свежей выдаче @{username}")

        # media_type: 2=видео, 1=фото, 8=карусель. Нашли пост и он НЕ видео — аудио точно нет,
        # но метрики поста есть — прокидываем их в NoAudioError, чтобы воркер записал статистику.
        media_type = item.get("media_type")
        if media_type != 2:
            photo_info: dict[str, Any] = {
                "id": shortcode,
                "uploader_id": username,
                "channel_follower_count": None,   # подписчиков дотянет воркер
                "view_count": item.get("play_count"),
                "like_count": item.get("like_count"),
                "comment_count": item.get("comment_count"),
                "description": (item.get("caption") or {}).get("text") if isinstance(item.get("caption"), dict) else item.get("caption"),
                "upload_date": _ts_to_yyyymmdd(item.get("taken_at")),
                "source": "apify-profile",
            }
            raise NoAudioError(
                f"{shortcode}: фото/карусель — нет аудио (media_type={media_type})",
                info=photo_info,
            )
        video_url = item.get("video_url")
        if not video_url:
            raise ProfileMissError(f"{shortcode}: видео без video_url — добиваю StarAPI")

        # Подписчиков здесь НЕ дёргаем из StarAPI (жгло квоту на каждом успешном рилсе).
        # Их дотягивает воркер централизованно через instaloader (см. workers/run.py).
        video_path = dest_dir / f"{shortcode}_prof.mp4"
        wav_path = dest_dir / f"{shortcode}_prof.wav"
        async with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with video_path.open("wb") as f:
                async for chunk in stream.aiter_bytes(8192):
                    f.write(chunk)

    await extract_wav_16k_mono(video_path, wav_path)

    info: dict[str, Any] = {
        "id": shortcode,
        "uploader_id": username,
        "channel_follower_count": None,   # подписчиков дотянет воркер через instaloader
        "view_count": item.get("play_count"),
        "like_count": item.get("like_count"),
        "comment_count": item.get("comment_count"),
        "description": (item.get("caption") or {}).get("text") if isinstance(item.get("caption"), dict) else item.get("caption"),
        "upload_date": _ts_to_yyyymmdd(item.get("taken_at")),
        "source": "apify-profile",
    }
    logger.info(
        "Apify-профиль ✓ %s (@%s views=%s likes=%s)",
        shortcode, username, info["view_count"], info["like_count"],
    )
    return wav_path, info
