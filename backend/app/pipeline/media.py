from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import imageio_ffmpeg


async def extract_wav_16k_mono(video_path: Path, wav_path: Path) -> None:
    """Извлечь аудио в WAV 16кГц моно через ffmpeg и удалить исходное видео.

    Общий хелпер для всех downloader'ов (StarAPI/Apify-профиль/instaloader) — раньше
    этот блок был продублирован в каждом. Видео удаляем сразу (правило «видео не хранить»).
    Бросает RuntimeError, если ffmpeg не смог извлечь дорожку.
    """
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
