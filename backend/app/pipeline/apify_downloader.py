from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_ACTOR = "apify~instagram-scraper"
_APIFY_BASE = "https://api.apify.com/v2"


async def fetch_batch_via_apify(
    urls: list[str], dest_dir: Path
) -> dict[str, tuple[Path, dict]]:
    """
    One Apify call for N URLs. Returns dict[shortcode → (wav_path, info)].
    Missing entries = download/scraping failed for that URL.
    """
    if not settings.apify_api_token:
        raise RuntimeError("APIFY_API_TOKEN не задан")
    if not urls:
        return {}

    logger.info("Apify batch (%d URLs)…", len(urls))
    input_scs = {_shortcode_from_url(u) for u in urls}

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{_APIFY_BASE}/acts/{_ACTOR}/run-sync-get-dataset-items",
            params={"token": settings.apify_api_token, "memory": 512, "timeout": 240},
            json={"directUrls": urls, "resultsType": "posts", "resultsLimit": len(urls)},
        )
        resp.raise_for_status()
        items: list[dict] = resp.json()

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
            raise RuntimeError(f"ffmpeg: {result.stderr[:200]}")

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
    if not settings.apify_api_token:
        raise RuntimeError("APIFY_API_TOKEN не задан — Apify-фолбэк недоступен")

    logger.info("Apify фолбэк: %s", url)

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{_APIFY_BASE}/acts/{_ACTOR}/run-sync-get-dataset-items",
            params={"token": settings.apify_api_token, "memory": 256, "timeout": 120},
            json={"directUrls": [url], "resultsType": "posts", "resultsLimit": 1},
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()

    if not items:
        raise RuntimeError(f"Apify вернул пустой результат для {url}")

    item = items[0]
    video_url: str = item.get("videoUrl") or item.get("video_url") or ""
    if not video_url:
        raise RuntimeError("Apify: поле videoUrl отсутствует в ответе")

    shortcode = _shortcode_from_url(url)
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
