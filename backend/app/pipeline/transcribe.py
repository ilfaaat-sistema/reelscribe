from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MLX_MODELS = {
    'medium':   'mlx-community/whisper-medium-mlx',
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
    'small':    'mlx-community/whisper-small-mlx',
}

_fw_cache: dict[str, object] = {}


def transcribe_audio(wav_path: Path, model_name: str = 'medium') -> tuple[str, str, float]:
    """Возвращает (text, language, duration_sec). Использует mlx-whisper на Apple Silicon."""
    try:
        return _transcribe_mlx(wav_path, model_name)
    except Exception as exc:
        logger.warning('mlx-whisper failed (%s), fallback to faster-whisper', exc)
        return _transcribe_fw(wav_path, model_name)


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


def _transcribe_fw(wav_path: Path, model_name: str) -> tuple[str, str, float]:
    if model_name not in _fw_cache:
        from faster_whisper import WhisperModel
        logger.info('Загружаю faster-whisper %s…', model_name)
        _fw_cache[model_name] = WhisperModel(model_name, device='cpu', compute_type='int8')
    model = _fw_cache[model_name]
    segments, info = model.transcribe(str(wav_path), beam_size=5)
    text = ' '.join(seg.text.strip() for seg in segments).strip()
    return text, info.language, info.duration
