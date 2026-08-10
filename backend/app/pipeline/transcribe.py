from __future__ import annotations

import importlib.util
import logging
import platform
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_MLX_MODELS = {
    'medium':   'mlx-community/whisper-medium-mlx',
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
    'small':    'mlx-community/whisper-small-mlx',
}

_LANG_NAME_TO_CODE = {
    'russian': 'ru', 'english': 'en', 'spanish': 'es', 'portuguese': 'pt',
    'french': 'fr', 'german': 'de', 'italian': 'it', 'ukrainian': 'uk',
    'turkish': 'tr', 'arabic': 'ar', 'hindi': 'hi', 'kazakh': 'kk',
}

_fw_cache: dict[str, object] = {}
_resolved_mode: str | None = None


def active_asr_mode() -> str:
    """Фактический ASR-режим: настройка asr_mode, в 'auto' — по окружению.

    auto: mlx на Apple Silicon → облачный API при наличии ключа → faster-whisper CPU.
    """
    global _resolved_mode
    if _resolved_mode is not None:
        return _resolved_mode

    mode = settings.asr_mode.lower()
    if mode != 'auto':
        _resolved_mode = mode
    elif (
        platform.system() == 'Darwin'
        and platform.machine() == 'arm64'
        and importlib.util.find_spec('mlx_whisper') is not None
    ):
        _resolved_mode = 'mlx'
    elif settings.openai_api_key:
        _resolved_mode = 'cloud'
    else:
        _resolved_mode = 'faster-whisper'
    return _resolved_mode


def transcribe_audio(wav_path: Path, model_name: str = 'medium') -> tuple[str, str, float, str]:
    """Возвращает (text, language, duration_sec, engine_used)."""
    mode = active_asr_mode()

    if mode == 'mlx':
        try:
            return (*_transcribe_mlx(wav_path, model_name), 'mlx-whisper')
        except Exception as exc:  # noqa: BLE001
            logger.warning('mlx-whisper failed (%s), fallback…', exc)
            mode = 'cloud' if settings.openai_api_key else 'faster-whisper'

    if mode == 'cloud':
        try:
            return (*_transcribe_cloud(wav_path), 'openai')
        except Exception as exc:  # noqa: BLE001
            logger.warning('облачный ASR failed (%s), fallback на faster-whisper…', exc)

    return (*_transcribe_fw(wav_path, model_name), 'faster-whisper')


def _transcribe_mlx(wav_path: Path, model_name: str) -> tuple[str, str, float]:
    import os
    import mlx_whisper
    import soundfile as sf
    import imageio_ffmpeg

    # создаём симлинк ffmpeg в /tmp чтобы mlx_whisper его нашёл по имени
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_link_dir = Path('/tmp/reelscribe_ffmpeg')
    ffmpeg_link_dir.mkdir(exist_ok=True)
    ffmpeg_link = ffmpeg_link_dir / 'ffmpeg'
    if not ffmpeg_link.exists():
        ffmpeg_link.symlink_to(ffmpeg_bin)
    os.environ['PATH'] = str(ffmpeg_link_dir) + os.pathsep + os.environ.get('PATH', '')

    hf_repo = _MLX_MODELS.get(model_name, _MLX_MODELS['medium'])
    logger.info('mlx-whisper %s → %s', model_name, hf_repo)

    result = mlx_whisper.transcribe(str(wav_path), path_or_hf_repo=hf_repo)
    text = result.get('text', '').strip()
    language = result.get('language', 'unknown')

    # длительность из аудиофайла
    try:
        info = sf.info(str(wav_path))
        duration = info.duration
    except Exception:
        duration = sum(s.get('end', 0) - s.get('start', 0) for s in result.get('segments', []))

    return text, language, float(duration)


def _transcribe_cloud(wav_path: Path) -> tuple[str, str, float]:
    """OpenAI Whisper API (whisper-1). Лимит файла 25 МБ — WAV 16кГц моно рилса влезает."""
    import httpx

    logger.info('облачный ASR (OpenAI whisper-1): %s', wav_path.name)
    with open(wav_path, 'rb') as f:
        resp = httpx.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {settings.openai_api_key}'},
            files={'file': (wav_path.name, f, 'audio/wav')},
            data={'model': 'whisper-1', 'response_format': 'verbose_json'},
            timeout=300,
        )
    resp.raise_for_status()
    d = resp.json()
    text = (d.get('text') or '').strip()
    # verbose_json отдаёт язык словом ('russian'), локальные движки — кодом ('ru');
    # без нормализации проверка language != 'ru' погнала бы русский текст на перевод
    language = _LANG_NAME_TO_CODE.get((d.get('language') or 'unknown').lower(),
                                      d.get('language', 'unknown'))
    return text, language, float(d.get('duration') or 0)


def _transcribe_fw(wav_path: Path, model_name: str) -> tuple[str, str, float]:
    if model_name not in _fw_cache:
        import os

        from faster_whisper import WhisperModel
        # Без явного cpu_threads CTranslate2 берёт ВСЕ ядра на каждый экземпляр модели. При
        # TRANSCRIBE_CONCURRENCY>1 это даёт переподписку по CPU и замедляет обе транскрипции,
        # поэтому делим ядра между параллельными экземплярами. При значении 1 (Mac) результат
        # совпадает с прежним неявным поведением.
        cpu_threads = max(1, (os.cpu_count() or 4) // max(1, settings.transcribe_concurrency))
        logger.info('Загружаю faster-whisper %s (cpu_threads=%d)…', model_name, cpu_threads)
        _fw_cache[model_name] = WhisperModel(
            model_name, device='cpu', compute_type='int8', cpu_threads=cpu_threads,
        )
    model = _fw_cache[model_name]
    segments, info = model.transcribe(str(wav_path), beam_size=5)
    text = ' '.join(seg.text.strip() for seg in segments).strip()
    return text, info.language, info.duration
