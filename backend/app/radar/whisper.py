"""Расшифровка речи через OpenAI Whisper API (Радар, итерация 2 — выбор движка).

В отличие от `gemini.py` (один вызов делает и расшифровку, и видеоанализ), здесь только
расшифровка: видео (mp4) отправляется в OpenAI как есть, отдельного извлечения аудио/ffmpeg
не делаем — то же самое уже проверено в старом локальном Радаре (см. CLAUDE.md проекта:
«Транскрибация — Этап 5») и в `app/pipeline/transcribe.py::_transcribe_cloud` этого репозитория,
там OpenAI молча принимает контейнер с видеодорожкой и берёт из него только звук.

Формат ответа `response_format=verbose_json` сверен с официальным API reference OpenAI
(2026-09-24, `developers.openai.com/api/docs/api-reference/audio/createTranscription`, доступ
к `platform.openai.com` для этого пути отдаёт 403 — редирект актуален): верхний уровень
содержит `language`, `duration`, `text`, `segments` (без явного `timestamp_granularities` —
дефолт для `whisper-1` уже сегменты, не слова), каждый элемент `segments` — как минимум
`start`, `end`, `text` (плюс служебные поля Whisper, которые нам не нужны).
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.radar import settings
from app.radar.gemini import (
    RateLimitedError,  # общий класс — pipeline ловит одним путём
)

logger = logging.getLogger(__name__)

_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


class WhisperUnavailableError(Exception):
    """Нет ключа OpenAI — Whisper недоступен как движок расшифровки."""


class WhisperFileTooLargeError(Exception):
    """Видео больше лимита OpenAI на файл транскрибации (WHISPER_MAX_BYTES)."""


class WhisperError(Exception):
    """Любая другая ошибка OpenAI (не 429, не превышение размера) — человекочитаемый текст."""


def _client_headers() -> dict:
    if not settings.OPENAI_API_KEY:
        raise WhisperUnavailableError("Не задан ключ OpenAI — Whisper недоступен")
    return {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}


def transcribe(video_path: Path) -> tuple[list[dict], str | None]:
    """Расшифровывает речь в видео целиком (OpenAI whisper-1, verbose_json).

    Возвращает (segments, language): `segments` — список {start, end, text} по возрастанию
    таймкодов (как отдаёт OpenAI, лишние служебные поля Whisper отбрасываем — контракт
    ТЗ 08 такой же, как у `gemini.analyze`'s `transcript_segments`); `language` — код или
    название языка из ответа, либо None, если OpenAI его не вернул.

    Синхронный вызов (httpx без async) — `pipeline.py` оборачивает в `asyncio.to_thread`,
    так же как `gemini.analyze`.
    """
    headers = _client_headers()

    size = video_path.stat().st_size
    if size > settings.WHISPER_MAX_BYTES:
        raise WhisperFileTooLargeError(
            f"Видео {size // (1024 * 1024)} МБ больше лимита Whisper "
            f"({settings.WHISPER_MAX_BYTES // (1024 * 1024)} МБ)"
        )

    try:
        with open(video_path, "rb") as f:
            resp = httpx.post(
                _TRANSCRIBE_URL,
                headers=headers,
                files={"file": (video_path.name, f, "video/mp4")},
                data={"model": settings.WHISPER_MODEL, "response_format": "verbose_json"},
                timeout=_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise WhisperError(f"Whisper: сетевая ошибка при обращении к OpenAI: {e}") from e

    if resp.status_code == 429:
        raise RateLimitedError(f"OpenAI Whisper: 429 {resp.text}"[:500])
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise WhisperError(f"Whisper: OpenAI ответил HTTP {resp.status_code}: {resp.text[:500]}") from e

    data = resp.json()
    segments = [
        {"start": seg.get("start"), "end": seg.get("end"), "text": (seg.get("text") or "").strip()}
        for seg in (data.get("segments") or [])
    ]
    language = data.get("language") or None
    return segments, language
