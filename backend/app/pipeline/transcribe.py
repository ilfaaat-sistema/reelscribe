from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_model_cache: dict[str, object] = {}


def _get_model(model_name: str) -> object:
    if model_name not in _model_cache:
        from faster_whisper import WhisperModel
        logger.info('Загружаю faster-whisper модель %s …', model_name)
        _model_cache[model_name] = WhisperModel(
            model_name, device='cpu', compute_type='int8'
        )
    return _model_cache[model_name]


def transcribe_audio(
    wav_path: Path,
    model_name: str = 'medium',
) -> tuple[str, str, float]:
    """Возвращает (text, language, duration_sec)."""
    from faster_whisper import WhisperModel
    model: WhisperModel = _get_model(model_name)  # type: ignore[assignment]
    segments, info = model.transcribe(str(wav_path), beam_size=5)
    text = ' '.join(seg.text.strip() for seg in segments).strip()
    return text, info.language, info.duration
