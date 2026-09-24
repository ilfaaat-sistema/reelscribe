"""Pydantic-схемы запросов Радара (docs/specs/08-radar.md).

Ответы намеренно НЕ типизируем response_model'ями: контракт задаёт точные формы ответов
(таблица эндпоинтов ТЗ), и лишний pydantic-слой рискует незаметно разойтись с ним по составу
полей — роутер отдаёт dict напрямую, форму держит app/radar/{analytics,repo,report}.py.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class ScrapeRequest(BaseModel):
    usernames: list[str]
    period_months: int = 3


class RefreshFollowersRequest(BaseModel):
    usernames: Optional[list[str]] = None


class AnalyzeItem(BaseModel):
    reel_id: str
    mode: Literal["t", "v", "tv"]


class AnalyzeRequest(BaseModel):
    items: list[AnalyzeItem]
    force: bool = False


class AnalyzeCancelRequest(BaseModel):
    reel_ids: list[str]
