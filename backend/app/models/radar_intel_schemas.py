"""Pydantic-схемы запросов разведки Радара (docs/specs/10-radar-intel.md).

Как и в radar_schemas.py, ответы не типизируются response_model'ями: форму держат
app/radar/feed.py и app/radar/sources.py (точные формы — раздел «Контракт» ТЗ).
"""
from __future__ import annotations

from pydantic import BaseModel


class AddSourcesRequest(BaseModel):
    input: str
