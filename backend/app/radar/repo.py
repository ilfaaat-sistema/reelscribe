"""Доступ к таблицам radar_* — вся работа с Supabase (`app.core.db.get_db()`) для Радара живёт
здесь, не в роутере и не в сервисах (docs/specs/08-radar.md, конвенция ReelScribe: SQL/доступ
к данным — не в роутерах).

Клиент supabase-py в этом проекте синхронный (см. app/core/db.py) — вызовы не await'ятся,
как и в остальном коде (app/api/reels.py, app/api/errors.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.db import get_db

# PostgREST молча режет .select() без .range() на ~1000 строк — постраничный сбор
# как в app/api/errors.py::_fetch_all_jobs.
_PAGE_SIZE = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── competitors ──────────────────────────────────────────────────────────────

def list_competitors() -> list[dict[str, Any]]:
    db = get_db()
    rows = (
        db.table("radar_competitors")
        .select("username, followers, followers_updated_at")
        .order("username")
        .execute()
    )
    return rows.data


def ensure_competitors(usernames: list[str]) -> None:
    """Заводит строки конкурентов, которых ещё нет (без followers) — INSERT только новых,
    чтобы не затирать уже собранных followers пустым upsert'ом."""
    if not usernames:
        return
    db = get_db()
    names = sorted({u for u in usernames if u})
    if not names:
        return
    existing = {
        r["username"]
        for r in db.table("radar_competitors").select("username").in_("username", names).execute().data
    }
    to_add = [u for u in names if u not in existing]
    if to_add:
        db.table("radar_competitors").insert([{"username": u} for u in to_add]).execute()


def upsert_competitor_followers(username: str, followers: Optional[int]) -> None:
    db = get_db()
    db.table("radar_competitors").upsert({
        "username": username,
        "followers": followers,
        "followers_updated_at": _now_iso(),
    }).execute()


# ── scrape_runs ──────────────────────────────────────────────────────────────

def count_runs_last_hour() -> int:
    """Считаем прогоны ОБОИХ kind (reels и followers) одним счётчиком — лимит в ТЗ describан
    как общая защита кошелька от Apify-вызовов, а не отдельно на каждый вид прогона."""
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = (
        db.table("radar_scrape_runs")
        .select("id", count="exact")
        .gte("created_at", since)
        .execute()
    )
    return rows.count or 0


def create_scrape_run(kind: str, usernames: list[str], period_months: Optional[int]) -> dict[str, Any]:
    db = get_db()
    row = db.table("radar_scrape_runs").insert({
        "kind": kind,
        "usernames": usernames,
        "period_months": period_months,
        "status": "pending",
    }).execute()
    return row.data[0]


def set_run_started(run_id: int, apify_run_id: str, apify_dataset_id: str, apify_token_ref: str) -> None:
    db = get_db()
    db.table("radar_scrape_runs").update({
        "status": "running",
        "apify_run_id": apify_run_id,
        "apify_dataset_id": apify_dataset_id,
        "apify_token_ref": apify_token_ref,
    }).eq("id", run_id).execute()


def get_scrape_run(run_id: int) -> Optional[dict[str, Any]]:
    db = get_db()
    rows = db.table("radar_scrape_runs").select("*").eq("id", run_id).execute()
    return rows.data[0] if rows.data else None


def finish_scrape_run_done(run_id: int, results_count: int, cost_estimate_usd: float) -> Optional[dict[str, Any]]:
    """Условный update status='running' → 'done': повторный вызов при уже завершённом прогоне
    не находит строку для апдейта (0 rows) и просто ничего не делает — идемпотентность приёма."""
    db = get_db()
    rows = (
        db.table("radar_scrape_runs")
        .update({
            "status": "done",
            "results_count": results_count,
            "cost_estimate_usd": cost_estimate_usd,
            "finished_at": _now_iso(),
        })
        .eq("id", run_id)
        .eq("status", "running")
        .execute()
    )
    return rows.data[0] if rows.data else None


def finish_scrape_run_error(run_id: int, error: str) -> None:
    db = get_db()
    db.table("radar_scrape_runs").update({
        "status": "error",
        "error": error[:2000],
        "finished_at": _now_iso(),
    }).eq("id", run_id).in_("status", ["pending", "running"]).execute()


# ── reels ──────────────────────────────────────────────────────────────────

def bulk_upsert_reels(reels: list[dict[str, Any]], scrape_run_id: Optional[int]) -> int:
    """Апсерт пачкой. Контракт: scrape_run_id НЕ затирается у уже существующих рилсов — поэтому
    строки делятся на «новые» (пишем scrape_run_id) и «уже были» (это поле не передаём вовсе),
    и каждая группа апсертится своим вызовом с одинаковым набором ключей у всех строк внутри.
    """
    if not reels:
        return 0
    db = get_db()
    ids = [r["id"] for r in reels]
    existing_ids: set[str] = set()
    for i in range(0, len(ids), 200):  # тот же приём чанков, что в app/api/errors.py
        chunk = ids[i:i + 200]
        existing_ids |= {
            row["id"] for row in db.table("radar_reels").select("id").in_("id", chunk).execute().data
        }

    now = _now_iso()
    new_rows: list[dict[str, Any]] = []
    existing_rows: list[dict[str, Any]] = []
    for r in reels:
        row = {**r, "updated_at": now}
        if r["id"] in existing_ids:
            existing_rows.append(row)
        else:
            row["scrape_run_id"] = scrape_run_id
            new_rows.append(row)

    if new_rows:
        db.table("radar_reels").upsert(new_rows, on_conflict="id").execute()
    if existing_rows:
        db.table("radar_reels").upsert(existing_rows, on_conflict="id").execute()
    return len(reels)


def get_reel(reel_id: str) -> Optional[dict[str, Any]]:
    db = get_db()
    rows = db.table("radar_reels").select("*").eq("id", reel_id).execute()
    return rows.data[0] if rows.data else None


def get_reels_by_ids(reel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not reel_ids:
        return {}
    db = get_db()
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(reel_ids), 200):
        chunk = reel_ids[i:i + 200]
        rows = db.table("radar_reels").select("*").in_("id", chunk).execute()
        out.update({r["id"]: r for r in rows.data})
    return out


def list_distinct_reel_usernames() -> list[str]:
    db = get_db()
    seen: list[str] = []
    seen_set: set[str] = set()
    offset = 0
    while True:
        page = (
            db.table("radar_reels")
            .select("username")
            .order("username")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
            .data
        )
        for r in page:
            u = r.get("username")
            if u and u not in seen_set:
                seen_set.add(u)
                seen.append(u)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return seen


def list_reels(
    usernames: list[str],
    min_views: Optional[int],
    max_views: Optional[int],
    sort_col: str,
    desc: bool,
    limit: int,
) -> list[dict[str, Any]]:
    db = get_db()
    q = db.table("radar_reels").select("*")
    if usernames:
        q = q.in_("username", usernames)
    if min_views is not None:
        q = q.gte("views", min_views)
    if max_views is not None:
        q = q.lt("views", max_views)
    q = q.order(sort_col, desc=desc).range(0, max(limit, 1) - 1)
    return q.execute().data


def list_reels_for_summary(usernames: list[str]) -> list[dict[str, Any]]:
    """Все рилсы (без ограничения limit) — нужно для медиан/сводки, а не только для ленты."""
    db = get_db()
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        q = db.table("radar_reels").select("id, username, views, likes, comments").order("id")
        if usernames:
            q = q.in_("username", usernames)
        page = q.range(offset, offset + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


# ── analyses / jobs ──────────────────────────────────────────────────────────

def get_analysis(reel_id: str) -> Optional[dict[str, Any]]:
    db = get_db()
    rows = db.table("radar_analyses").select("*").eq("reel_id", reel_id).execute()
    return rows.data[0] if rows.data else None


def get_analyses_by_ids(reel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not reel_ids:
        return {}
    db = get_db()
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(reel_ids), 200):
        chunk = reel_ids[i:i + 200]
        rows = db.table("radar_analyses").select("*").in_("reel_id", chunk).execute()
        out.update({r["reel_id"]: r for r in rows.data})
    return out


def list_analyses_status(reel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not reel_ids:
        return {}
    db = get_db()
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(reel_ids), 200):
        chunk = reel_ids[i:i + 200]
        rows = (
            db.table("radar_analyses")
            .select("reel_id, mode, status")
            .in_("reel_id", chunk)
            .execute()
        )
        out.update({r["reel_id"]: r for r in rows.data})
    return out


def upsert_analysis_queued(reel_id: str, mode: str) -> None:
    db = get_db()
    db.table("radar_analyses").upsert({
        "reel_id": reel_id,
        "mode": mode,
        "status": "queued",
        "updated_at": _now_iso(),
    }).execute()


def create_job(reel_id: str, mode: str) -> dict[str, Any]:
    db = get_db()
    row = db.table("radar_jobs").insert({
        "reel_id": reel_id,
        "mode": mode,
        "state": "queued",
    }).execute()
    return row.data[0]


def count_jobs_created_today() -> int:
    db = get_db()
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = db.table("radar_jobs").select("id", count="exact").gte("created_at", since).execute()
    return rows.count or 0


def cancel_jobs(reel_ids: list[str]) -> list[str]:
    """Отменяет ещё не завершённые задания (queued/in_progress) и соответствующие анализы.
    Возвращает id рилсов, чьи задания реально были отменены."""
    if not reel_ids:
        return []
    db = get_db()
    now = _now_iso()
    jobs = (
        db.table("radar_jobs")
        .update({"state": "cancelled", "updated_at": now})
        .in_("reel_id", reel_ids)
        .in_("state", ["queued", "in_progress"])
        .execute()
    )
    cancelled_ids = sorted({j["reel_id"] for j in jobs.data})
    if cancelled_ids:
        db.table("radar_analyses").update({
            "status": "cancelled",
            "updated_at": now,
        }).in_("reel_id", cancelled_ids).in_(
            "status", ["queued", "downloading", "analyzing", "rate_limited"]
        ).execute()
    return cancelled_ids
