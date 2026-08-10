from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.pipeline.apify_client import run_actor_get_items
from app.pipeline.media import extract_wav_16k_mono

logger = logging.getLogger(__name__)

_ACTOR = "apify~instagram-scraper"


async def fetch_batch_via_apify(
    urls: list[str], dest_dir: Path
) -> dict[str, tuple[Path, dict]]:
    """
    One Apify call for N URLs. Returns dict[shortcode → (wav_path, info)].
    Missing entries = download/scraping failed for that URL.
    """
    if not settings.apify_token_list:
        raise RuntimeError("APIFY_API_TOKEN не задан")
    if not urls:
        return {}

    logger.info("Apify batch (%d URLs)…", len(urls))
    input_scs = {_shortcode_from_url(u) for u in urls}

    async with httpx.AsyncClient(timeout=300) as client:
        items: list[dict] = await run_actor_get_items(
            client, _ACTOR,
            {"directUrls": urls, "resultsType": "posts", "resultsLimit": len(urls)},
            extra_params={"memory": 512, "timeout": 240},
        )

    # Match Apify results to input shortcodes
    matched: dict[str, dict] = {}
    for item in items:
        sc = item.get("shortCode") or item.get("id") or ""
        if sc in input_scs:
            matched[sc] = item

    if not matched:
        logger.warning("Apify batch: нет совпадений в ответе (%d items)", len(items))
        return {}

    # Download all videos in parallel
    downloads = await asyncio.gather(
        *[_dl_one(sc, item, dest_dir) for sc, item in matched.items()],
        return_exceptions=False,
    )
    return {sc: (wav, info) for sc, wav, info in downloads if wav is not None}


async def _dl_one(
    sc: str, item: dict, dest_dir: Path
) -> tuple[str, Path | None, dict | None]:
    """Download one video from Apify result and extract WAV audio."""
    video_url = item.get("videoUrl") or item.get("video_url") or ""
    if not video_url:
        logger.warning("Apify batch: no videoUrl for %s", sc)
        return sc, None, None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        video_path = dest_dir / f"{sc}_apify.mp4"
        wav_path = dest_dir / f"{sc}_apify.wav"

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as cl:
            async with cl.stream("GET", video_url) as stream:
                stream.raise_for_status()
                with video_path.open("wb") as f:
                    async for chunk in stream.aiter_bytes(8192):
                        f.write(chunk)

        await extract_wav_16k_mono(video_path, wav_path)

        info: dict[str, Any] = {
            "id": sc,
            "uploader_id": item.get("ownerUsername"),
            "channel_follower_count": item.get("ownerFollowersCount"),
            "view_count": item.get("videoViewCount"),
            "like_count": item.get("likesCount"),
            "comment_count": item.get("commentsCount"),
            "description": item.get("caption"),
            "upload_date": _parse_ts(item.get("timestamp")),
        }
        logger.info("Apify batch ✓ %s (views=%s likes=%s)", sc,
                    info.get("view_count"), info.get("like_count"))
        return sc, wav_path, info
    except Exception as exc:
        logger.error("Apify batch ✗ %s: %s", sc, exc)
        return sc, None, None


async def fetch_via_apify(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Single-URL Apify fallback (used when batch prefetch missed this URL)."""
    if not settings.apify_token_list:
        raise RuntimeError("APIFY_API_TOKEN не задан — Apify-фолбэк недоступен")

    logger.info("Apify фолбэк: %s", url)

    async with httpx.AsyncClient(timeout=180) as client:
        items: list[dict[str, Any]] = await run_actor_get_items(
            client, _ACTOR,
            {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1},
            extra_params={"memory": 256, "timeout": 120},
        )

    if not items:
        raise RuntimeError(f"Apify вернул пустой результат для {url}")

    item = items[0]
    shortcode = _shortcode_from_url(url)
    video_url: str = item.get("videoUrl") or item.get("video_url") or ""
    if not video_url:
        # Пост-картинка/карусель: видео нет, но метрики есть — отдаём их через NoAudioError,
        # чтобы воркер записал статистику, а рилс пометил как no_audio (а не «ошибка»).
        from app.pipeline.apify_profile_downloader import NoAudioError
        photo_info: dict[str, Any] = {
            "id": shortcode,
            "uploader_id": item.get("ownerUsername"),
            "channel_follower_count": item.get("ownerFollowersCount"),
            "view_count": item.get("videoViewCount"),
            "like_count": item.get("likesCount"),
            "comment_count": item.get("commentsCount"),
            "description": item.get("caption"),
            "upload_date": _parse_ts(item.get("timestamp")),
        }
        raise NoAudioError(f"{shortcode}: пост без видео (фото/карусель)", info=photo_info)
    sc, wav_path, info = await _dl_one(shortcode, item, dest_dir)
    if wav_path is None:
        raise RuntimeError(f"Apify: не удалось скачать аудио для {url}")
    return wav_path, info  # type: ignore[return-value]


def _shortcode_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _parse_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return ts[:10].replace("-", "")
    except Exception:
        return None
