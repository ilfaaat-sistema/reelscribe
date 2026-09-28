"""`POST /api/chat` — ИИ-чат по всей базе (ТЗ 09, `docs/specs/09-ai-chat.md`).

Тонкий роутер: валидация запроса + вызов `app.chat.agent.run_agent`. Rate-limit — СВОЯ копия
`_check_rate_limit` из `app/api/import_.py` с ключом `'chat:' + ip` (30 вопросов за 10 минут),
чтобы не делить лимит с импортом — вынести общую функцию нельзя без правки import_.py, а трогать
его вне этой задачи не нужно.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.chat.agent import ChatAgentError, run_agent
from app.core.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

RATE_MAX_QUESTIONS = 30
RATE_WINDOW_SEC = 600
RATE_IP_PREFIX = "chat:"
MAX_HISTORY_ITEMS = 10


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str = ""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def _strip_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message пустой")
        return v


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    """Копия `_check_rate_limit` из `app/api/import_.py`, но со своим ключом и своими лимитами —
    вопросы чата не должны есть лимит импорта (5 за 10 мин) и наоборот."""
    key = RATE_IP_PREFIX + ip
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=RATE_WINDOW_SEC)
    ).isoformat()
    db = get_db()
    try:
        db.table("rate_limits").delete().lt("created_at", cutoff).execute()
        recent = (
            db.table("rate_limits").select("id")
            .eq("ip", key).gte("created_at", cutoff)
            .execute().data
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rate-limit чата не проверен (%s) — пропускаю проверку", exc)
        return

    if len(recent) >= RATE_MAX_QUESTIONS:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много вопросов подряд (лимит {RATE_MAX_QUESTIONS} за "
                   f"{RATE_WINDOW_SEC // 60} минут). Подожди немного и попробуй снова.",
        )
    db.table("rate_limits").insert({"ip": key}).execute()


@router.post("/chat")
async def chat(body: ChatRequest, request: Request) -> dict:
    _check_rate_limit(_client_ip(request))

    history = [{"role": m.role, "text": m.text} for m in body.history[-MAX_HISTORY_ITEMS:]]

    try:
        result = run_agent(body.message, history)
    except ChatAgentError as exc:
        raise HTTPException(status_code=502, detail=f"Модель недоступна: {exc}") from exc

    return result
