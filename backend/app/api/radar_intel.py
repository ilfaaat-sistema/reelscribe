"""Роутер /api/radar/{feed,sources}* — разведка: лента залётных и источники
(docs/specs/10-radar-intel.md). Тонкий роутер: валидация и вызов app/radar/{feed,sources}.py."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.models.radar_intel_schemas import AddSourcesRequest
from app.radar import feed, sources

router = APIRouter(prefix="/radar", tags=["radar-intel"])

_PERIODS = (0, 7, 30, 90)


@router.get("/feed")
async def get_feed(
    period: int = Query(30),
    usernames: Optional[str] = Query(None),
    only_viral: int = Query(0),
    sort: str = Query("viral"),
    order: str = Query("desc"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if period not in _PERIODS:
        raise HTTPException(400, "Период — 7, 30, 90 дней или 0 (всё время)")
    names = [sources.normalize_username(u) for u in (usernames or "").split(",") if u.strip()]
    return feed.build_feed(
        period=period,
        usernames=names,
        only_viral=bool(only_viral),
        sort=feed.normalize_sort(sort),
        order="asc" if order == "asc" else "desc",
        limit=limit,
        offset=offset,
    )


@router.get("/sources")
async def get_sources() -> dict[str, Any]:
    return feed.build_sources()


@router.post("/sources")
async def add_sources(req: AddSourcesRequest) -> dict[str, Any]:
    if not req.input.strip():
        raise HTTPException(400, "Введите ник или ссылку на профиль")
    try:
        return sources.add_sources(req.input)
    except sources.SourceLimitError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/sources/{username}")
async def remove_source(username: str) -> dict[str, Any]:
    try:
        sources.exclude_source(username)
    except sources.SourceNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"removed": True}


@router.post("/sources/{username}/restore")
async def restore_source(username: str) -> dict[str, Any]:
    try:
        sources.restore_source(username)
    except sources.SourceNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except sources.SourceLimitError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"restored": True}
