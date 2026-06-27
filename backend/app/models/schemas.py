from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel


# ── Import ────────────────────────────────────────────────────────────────────

class ImportRequest(BaseModel):
    links_text: str
    source_type: Literal["paste", "txt", "csv", "json"] = "paste"
    engine: Literal["faster-whisper", "yandex", "deepgram", "openai"] = "faster-whisper"
    model: Optional[Literal["medium", "large-v3"]] = "medium"
    translate: bool = False
    pull_stats: bool = True
    comment: Optional[str] = None


class ImportResponse(BaseModel):
    session_id: UUID
    total: int
    reels_count: int
    posts_count: int
    duplicates: int


# ── Sessions ──────────────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    id: UUID
    created_at: datetime
    source_type: str
    engine: str
    model: Optional[str]
    translate: bool
    pull_stats: bool
    comment: Optional[str]
    total: int
    status: str


class SessionDetail(SessionSummary):
    loaded: int
    failed: int
    done_ids: list[UUID]


class SessionCommentUpdate(BaseModel):
    comment: str


# ── Reels ─────────────────────────────────────────────────────────────────────

class ReelRow(BaseModel):
    id: UUID
    shortcode: str
    url: str
    type: str
    caption: Optional[str]           # текст поста (подпись автора)
    author_handle: Optional[str]
    author_followers: Optional[int]
    views: Optional[int]
    likes: Optional[int]
    comments: Optional[int]
    posted_at: Optional[date]
    er: Optional[float]
    lpf: Optional[float]
    cpf: Optional[float]
    eng: Optional[float]
    transcript_status: Optional[str]
    transcript_text: Optional[str]   # расшифровка речи (НЕ caption)
    transcript_text_ru: Optional[str]
    has_note: bool = False


class ReelDetail(ReelRow):
    transcript_language: Optional[str]
    transcript_duration_sec: Optional[int]
    summary: Optional[str]
    tags: Optional[list[str]]
    note: Optional[str]


class NoteUpdate(BaseModel):
    note: str


# ── Export ────────────────────────────────────────────────────────────────────

class ExportParams(BaseModel):
    format: Literal["csv", "xlsx", "json", "md"] = "csv"
    lang: Literal["orig", "ru", "both"] = "ru"
    scope: Literal["all", "selected"] = "all"
    ids: Optional[list[UUID]] = None


# ── Progress ──────────────────────────────────────────────────────────────────

class ProgressResponse(BaseModel):
    session_id: UUID
    loaded: int
    total: int
    failed: int
    done_ids: list[UUID]
    failed_ids: list[UUID] = []
