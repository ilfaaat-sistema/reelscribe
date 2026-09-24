"""Расшифровка речи и видеоанализ ОДНИМ вызовом Gemini (Радар, этап разбора).

В отличие от старого локального Радара (Whisper отдельно + Gemini отдельно), здесь
Whisper/ffmpeg не используются вовсе: за один запрос к `gemini-2.5-flash` модель и
расшифровывает речь с таймкодами, и разбирает видеоряд — экономит время в лимите
Vercel (300 с) и убирает зависимость от системного ffmpeg на сервере.

Только Gemini API по ключу (`GEMINI_API_KEY`) — без Vertex: Vercel работает из США,
блокировки по локации, из-за которой Vertex понадобился на Маке (см. CLAUDE.md
проекта), там нет.

API SDK сверен с фактически установленной версией `google-genai==1.47.0` в venv проекта
(`client.models.generate_content`, `types.Part.from_bytes`, `types.GenerateContentConfig`
с `response_mime_type`/`response_schema`, `client.files.upload/get/delete`) — прямой
интроспекцией пакета, а не по памяти: часть найденной через WebFetch документации
описывала более новый «Interactions API» (`client.interactions.create`), которого в
установленной версии SDK нет.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

from app.radar import settings

logger = logging.getLogger(__name__)

# Files API: сколько ждём перехода загруженного файла в состояние ACTIVE.
_FILES_ACTIVE_TIMEOUT_SEC = 120
_FILES_POLL_INTERVAL_SEC = 3

# Запас поверх длительности видео при проверке таймкодов расшифровки — так в ТЗ ("+1 с").
_SEGMENT_TOLERANCE_SEC = 1.0


class RateLimitedError(Exception):
    """Gemini ответил 429/RESOURCE_EXHAUSTED — временное ограничение, не финальная ошибка.

    pipeline.py ловит это исключение отдельно: задание возвращается в очередь
    (`radar_jobs.state='queued'`, `next_attempt_at=now()+60s`), а не проваливается.
    """


class ValidationError(Exception):
    """Таймкоды в ответе Gemini не прошли проверку даже после повторной попытки."""


class _TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class _VisualFrame(BaseModel):
    t: str
    text: str


class _AnalysisSchema(BaseModel):
    """Схема JSON-ответа Gemini.

    `video_duration_sec` нужен только для проверки таймкодов расшифровки — наружу
    (в словарь, который возвращает `analyze()`) не идёт: это не часть контракта
    ТЗ 08 для `pipeline.py`, а внутренняя деталь валидации.
    """

    transcript_segments: list[_TranscriptSegment] = Field(default_factory=list)
    transcript_language: str = ""
    visual_timeline: list[_VisualFrame] = Field(default_factory=list)
    hook: str = ""
    structure: str = ""
    cta: str = ""
    format_idea: str = ""
    video_duration_sec: float = 0.0


_BASE_PROMPT = (
    "Ты — продюсер коротких видео. Проанализируй этот Instagram Reels-ролик и верни "
    "строго JSON по заданной схеме, без markdown-обёртки и лишнего текста."
)

_TRANSCRIPT_PROMPT = (
    "Расшифруй речь в видео построчно — раздели на сегменты по фразам/предложениям. "
    "Для каждого сегмента укажи start и end в секундах от начала видео с точностью до "
    "десятых (например 12.4). transcript_language — код языка распознанной речи "
    "(ru, en, ...). Если речи нет — пустой список сегментов и transcript_language \"\"."
)

_VISUAL_PROMPT = (
    "Опиши визуальный ряд по кадрам в visual_timeline: t в формате \"0:00-0:02\", text — "
    "что происходит (локация, люди, действия, текст-плашки, графика, монтаж). "
    "hook — что удерживает внимание в первые 1-3 секунды. "
    "structure — тайминг-структура хук → блоки → CTA, с секундами. "
    "cta — призыв к действию, если есть. "
    "format_idea — как адаптировать формат под другую нишу, 1-2 предложения."
)

_DURATION_PROMPT = "video_duration_sec — длительность видео в секундах, с десятыми."


def _build_prompt(mode: str) -> str:
    parts = [_BASE_PROMPT]
    if mode in ("t", "tv"):
        parts.append(_TRANSCRIPT_PROMPT)
    if mode in ("v", "tv"):
        parts.append(_VISUAL_PROMPT)
    parts.append(_DURATION_PROMPT)
    return "\n\n".join(parts)


def _client():
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("Не задан GEMINI_API_KEY — видеоанализ недоступен")
    from google import genai

    return genai.Client(api_key=settings.GEMINI_API_KEY)


def _wait_active(client, name: str):
    f = client.files.get(name=name)
    deadline = time.monotonic() + _FILES_ACTIVE_TIMEOUT_SEC
    while True:
        state = f.state.name if hasattr(f.state, "name") else str(f.state)
        if state == "ACTIVE":
            return f
        if state == "FAILED":
            raise RuntimeError("Gemini не смог обработать загруженное видео")
        if time.monotonic() >= deadline:
            raise TimeoutError("Gemini: файл не перешёл в состояние ACTIVE за отведённое время")
        time.sleep(_FILES_POLL_INTERVAL_SEC)
        f = client.files.get(name=name)


def _parse_response(response) -> _AnalysisSchema:
    if isinstance(response.parsed, _AnalysisSchema):
        return response.parsed
    # Фолбэк на случай, если SDK не собрал pydantic-объект сам (response.parsed пуст) —
    # разбираем JSON из текста руками.
    return _AnalysisSchema.model_validate_json(response.text)


def _generate(client, video_path: Path, mode: str) -> _AnalysisSchema:
    from google.genai import types

    prompt = _build_prompt(mode)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_AnalysisSchema,
    )

    size = video_path.stat().st_size
    if size < settings.INLINE_MAX_BYTES:
        video_part = types.Part.from_bytes(data=video_path.read_bytes(), mime_type="video/mp4")
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[video_part, prompt],
            config=config,
        )
        return _parse_response(response)

    uploaded = client.files.upload(file=video_path)
    try:
        active_file = _wait_active(client, uploaded.name)
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[active_file, prompt],
            config=config,
        )
        return _parse_response(response)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:  # noqa: BLE001 — удаление лучшим усилием, не должно ронять анализ
            logger.warning("Gemini: не удалось удалить временный файл %s: %s", uploaded.name, e)


def _validate(result: _AnalysisSchema, mode: str, known_duration: float | None = None) -> None:
    """Сегменты расшифровки — по возрастанию таймкодов и не дальше длительности видео (+1 с)."""
    if mode not in ("t", "tv"):
        return
    segments = result.transcript_segments
    if not segments:
        return

    # Длительность из Apify надёжнее той, что Gemini сообщает о видео сам
    duration = known_duration or result.video_duration_sec or 0.0
    max_end = duration + _SEGMENT_TOLERANCE_SEC if duration > 0 else None

    prev_start = -1.0
    for seg in segments:
        if seg.end < seg.start:
            raise ValidationError(f"сегмент с end < start: {seg.start}-{seg.end}")
        if seg.start < prev_start:
            raise ValidationError("таймкоды сегментов идут не по возрастанию")
        if max_end is not None and seg.end > max_end:
            raise ValidationError(
                f"сегмент {seg.start}-{seg.end} выходит за длительность видео ({duration} с)"
            )
        prev_start = seg.start


def _attempt_with_validation(
    client, video_path: Path, mode: str, known_duration: float | None = None
) -> _AnalysisSchema:
    result = _generate(client, video_path, mode)
    try:
        _validate(result, mode, known_duration)
        return result
    except ValidationError as e:
        logger.warning("Gemini: таймкоды не прошли проверку (%s) — повторяю запрос", e)
        result = _generate(client, video_path, mode)
        _validate(result, mode, known_duration)  # вторая ошибка уже не гасится — поднимается вызывающему
        return result


def _is_rate_limited(exc: Exception) -> bool:
    from google.genai import errors

    if isinstance(exc, errors.APIError) and (exc.code == 429 or (exc.status or "") == "RESOURCE_EXHAUSTED"):
        return True
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def _to_contract_dict(result: _AnalysisSchema, mode: str) -> dict:
    transcript_segments = None
    transcript_language = None
    visual_timeline = None
    hook = structure = cta = format_idea = None

    if mode in ("t", "tv"):
        transcript_segments = [s.model_dump() for s in result.transcript_segments]
        transcript_language = result.transcript_language or None
    if mode in ("v", "tv"):
        visual_timeline = [v.model_dump() for v in result.visual_timeline]
        hook = result.hook or None
        structure = result.structure or None
        cta = result.cta or None
        format_idea = result.format_idea or None

    return {
        "transcript_segments": transcript_segments,
        "transcript_language": transcript_language,
        "visual_timeline": visual_timeline,
        "hook": hook,
        "structure": structure,
        "cta": cta,
        "format_idea": format_idea,
    }


def analyze(video_path: Path, mode: str, duration_sec: float | None = None) -> dict:
    """Расшифровка речи и/или видеоанализ ОДНИМ вызовом Gemini. `mode`: `t` | `v` | `tv`.

    Для `t` визуальные поля (visual_timeline, hook, structure, cta, format_idea) — None.
    Для `v` поля расшифровки (transcript_segments, transcript_language) — None.
    Видео < `INLINE_MAX_BYTES` уходит в Gemini inline, иначе — через Files API
    (ожидание ACTIVE, удаление файла в finally — он не должен оставаться на стороне Google).
    """
    client = _client()
    try:
        result = _attempt_with_validation(client, video_path, mode, duration_sec)
    except Exception as e:
        if isinstance(e, ValidationError):
            raise
        if _is_rate_limited(e):
            raise RateLimitedError(str(e)) from e
        raise

    return _to_contract_dict(result, mode)
