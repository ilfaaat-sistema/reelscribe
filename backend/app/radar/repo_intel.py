"""Доступ к данным разведки Радара (лента и источники) — docs/specs/10-radar-intel.md.

Вся работа с Supabase для ленты/источников живёт здесь. radar/repo.py намеренно не
импортируется (его параллельно правит другой исполнитель) — постраничный сбор и _now_iso
повторены. Клиент supabase синхронный.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.db import get_db

# PostgREST молча режет выборку без .range() на 1000 строк — собираем страницами.
_PAGE_SIZE = 1000
_CHUNK = 100  # размер пачки id в .in_() (длина URL)

_COMPETITOR_COLS = (
    "username, followers, followers_updated_at, full_name, is_source, "
    "source_added_at, excluded_at, last_scraped_at"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(items: list[Any], size: int = _CHUNK) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── источники ────────────────────────────────────────────────────────────────

def list_active_sources() -> list[dict[str, Any]]:
    db = get_db()
    rows = (
        db.table("radar_competitors")
        .select(_COMPETITOR_COLS)
        .eq("is_source", True)
        .order("username")
        .execute()
    )
    return rows.data


def list_excluded_sources() -> list[dict[str, Any]]:
    db = get_db()
    rows = (
        db.table("radar_competitors")
        .select(_COMPETITOR_COLS)
        .eq("is_source", False)
        .not_.is_("excluded_at", "null")
        .order("excluded_at", desc=True)
        .execute()
    )
    return rows.data


def count_active_sources() -> int:
    db = get_db()
    rows = db.table("radar_competitors").select("username", count="exact").eq("is_source", True).execute()
    return rows.count or 0


def get_competitors(usernames: list[str]) -> dict[str, dict[str, Any]]:
    """Строки radar_competitors по списку ников (любого состояния)."""
    out: dict[str, dict[str, Any]] = {}
    if not usernames:
        return out
    db = get_db()
    for chunk in _chunks(usernames):
        rows = db.table("radar_competitors").select(_COMPETITOR_COLS).in_("username", chunk).execute()
        out.update({r["username"]: r for r in rows.data})
    return out


def upsert_source(username: str) -> None:
    """Добавляет/возвращает источник. followers и прочие поля не передаются — не затираются."""
    db = get_db()
    db.table("radar_competitors").upsert({
        "username": username,
        "is_source": True,
        "source_added_at": _now_iso(),
        "excluded_at": None,
    }, on_conflict="username").execute()


def mark_excluded(username: str) -> bool:
    """Убирает активный источник в исключённые. False — такого активного источника нет."""
    db = get_db()
    rows = (
        db.table("radar_competitors")
        .update({"is_source": False, "excluded_at": _now_iso()})
        .eq("username", username)
        .eq("is_source", True)
        .execute()
    )
    return bool(rows.data)


def mark_restored(username: str) -> bool:
    """Возвращает исключённый источник. False — такого исключённого нет."""
    db = get_db()
    rows = (
        db.table("radar_competitors")
        .update({"is_source": True, "excluded_at": None})
        .eq("username", username)
        .eq("is_source", False)
        .not_.is_("excluded_at", "null")
        .execute()
    )
    return bool(rows.data)


# ── рилсы ────────────────────────────────────────────────────────────────────

def list_light_reels(usernames: list[str]) -> list[dict[str, Any]]:
    """Лёгкая выборка рилсов указанных аккаунтов (без caption/ссылок) — для нормы и P70."""
    if not usernames:
        return []
    db = get_db()
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = (
            db.table("radar_reels")
            .select("id, username, views, likes, comments, posted_at")
            .in_("username", usernames)
            .order("id")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
            .data
        )
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def get_full_reels(reel_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not reel_ids:
        return out
    db = get_db()
    for chunk in _chunks(reel_ids, 100):
        rows = db.table("radar_reels").select("*").in_("id", chunk).execute()
        out.update({r["id"]: r for r in rows.data})
    return out


def list_snapshots(reel_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Снимки метрик по рилсам (reel_id → список по возрастанию taken_at). Вызывающий
    передаёт только рилсы младше 14 дней. Каждая пачка id собирается постранично."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not reel_ids:
        return out
    db = get_db()
    for chunk in _chunks(reel_ids, 100):
        offset = 0
        while True:
            page = (
                db.table("radar_reel_snapshots")
                .select("reel_id, views, likes, comments, taken_at")
                .in_("reel_id", chunk)
                .order("id")
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
                .data
            )
            for s in page:
                out.setdefault(s["reel_id"], []).append(s)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    for lst in out.values():
        lst.sort(key=lambda s: s.get("taken_at") or "")
    return out


def list_analyses_status(reel_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not reel_ids:
        return out
    db = get_db()
    for chunk in _chunks(reel_ids, 200):
        rows = db.table("radar_analyses").select("reel_id, mode, status").in_("reel_id", chunk).execute()
        out.update({r["reel_id"]: r for r in rows.data})
    return out


def get_last_refresh_at() -> Optional[str]:
    """Время завершения последнего успешного обновления (radar_refreshes.status='done')."""
    db = get_db()
    rows = (
        db.table("radar_refreshes")
        .select("finished_at")
        .eq("status", "done")
        .not_.is_("finished_at", "null")
        .order("finished_at", desc=True)
        .limit(1)
        .execute()
    )
    return rows.data[0]["finished_at"] if rows.data else None

