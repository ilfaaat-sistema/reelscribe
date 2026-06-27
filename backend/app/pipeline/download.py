from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import yt_dlp

from app.core.config import settings

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(2)   # max 2 параллельных скачивания


def _get_ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


async def download_audio(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    async with _semaphore:
        try:
            path, info = await asyncio.to_thread(_download_sync, url, dest_dir)
        except Exception as exc:
            if settings.apify_api_token:
                logger.warning("yt-dlp: %s — пробую Apify-фолбэк…", exc)
                from app.pipeline.apify_downloader import fetch_via_apify
                path, info = await fetch_via_apify(url, dest_dir)
            else:
                raise
        await asyncio.sleep(random.uniform(1.5, 4.0))   # throttle jitter
    return path, info


def _download_sync(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    ffmpeg_exe = _get_ffmpeg()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)

    ydl_opts: dict[str, Any] = {
        'format': 'bestaudio/best',
        'outtmpl': str(dest_dir / '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'ffmpeg_location': ffmpeg_dir,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
            'preferredquality': '0',
        }],
        'postprocessor_args': {'ffmpeg': ['-ar', '16000', '-ac', '1']},
        'ignoreerrors': False,
        'retries': 3,
    }

    if settings.instagram_cookies_file:
        ydl_opts['cookiefile'] = settings.instagram_cookies_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError(f'yt-dlp вернул None для {url}')

    video_id = info.get('id', '')
    wav_candidates = list(dest_dir.glob(f'{video_id}*.wav'))
    if not wav_candidates:
        wav_candidates = sorted(dest_dir.glob('*.wav'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wav_candidates:
        raise FileNotFoundError(f'WAV файл не найден после скачивания {url}')

    return wav_candidates[0], info
