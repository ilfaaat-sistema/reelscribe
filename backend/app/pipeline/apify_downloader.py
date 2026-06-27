from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_ACTOR = "apify~instagram-scraper"
_APIFY_BASE = "https://api.apify.com/v2"


async def fetch_via_apify(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Фолбэк скачивания через Apify когда yt-dlp заблокирован."""
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
        raise RuntimeError(f"Apify: поле videoUrl отсутствует в ответе")

    dest_dir.mkdir(parents=True, exist_ok=True)
    shortcode = _shortcode_from_url(url)
    video_path = dest_dir / f"{shortcode}_apify.mp4"
    wav_path = dest_dir / f"{shortcode}_apify.wav"

    # скачиваем CDN-видео напрямую
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with video_path.open("wb") as f:
                async for chunk in stream.aiter_bytes(8192):
                    f.write(chunk)

    # извлекаем аудио через ffmpeg
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-y", "-i", str(video_path),
         "-ar", "16000", "-ac", "1", str(wav_path)],
        capture_output=True, text=True,
    )
    video_path.unlink(missing_ok=True)

    if result.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"ffmpeg не извлёк аудио: {result.stderr[:300]}")

    info: dict[str, Any] = {
        "id": shortcode,
        "uploader_id": item.get("ownerUsername"),
        "channel_follower_count": item.get("ownerFollowersCount"),
        "view_count": item.get("videoViewCount"),
        "like_count": item.get("likesCount"),
        "comment_count": item.get("commentsCount"),
        "description": item.get("caption"),
        "upload_date": _parse_ts(item.get("timestamp")),
    }
    logger.info("Apify скачал %s, метаданные: views=%s likes=%s",
                shortcode, info.get("view_count"), info.get("like_count"))
    return wav_path, info


def _shortcode_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _parse_ts(ts: str | None) -> str | None:
    # "2024-04-10T14:30:00.000Z" → "20240410"
    if not ts:
        return None
    try:
        return ts[:10].replace("-", "")
    except Exception:
        return None
