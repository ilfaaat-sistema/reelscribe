"""Роутер обновления источников Радара (ТЗ 10): оценка, запуск, шаг конвейера.

Тонкий роутер — вся логика в app/radar/refresh_service.py. Ошибки — HTTPException с русским
detail для показа в интерфейсе."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.radar import refresh_service

router = APIRouter(prefix="/radar", tags=["radar-refresh"])


class RefreshStartRequest(BaseModel):
    force: bool = False


@router.get("/refresh/estimate")
async def refresh_estimate() -> dict[str, Any]:
    return refresh_service.estimate()


@router.post("/refresh")
async def refresh_start(req: Optional[RefreshStartRequest] = None) -> dict[str, Any]:
    try:
        refresh_id = refresh_service.start(force=bool(req and req.force))
    except refresh_service.RefreshValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except refresh_service.RefreshBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except refresh_service.RefreshLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"refresh_id": refresh_id}


@router.get("/refresh/current")
async def refresh_current() -> dict[str, Any]:
    return await refresh_service.current_step()
