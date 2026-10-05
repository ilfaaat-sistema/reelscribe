"""Обновление источников Радара кнопкой (ТЗ 10, docs/specs/10-radar-intel.md, раздел «Обновление»).

Конвейер без cron: прогресс двигает каждый GET /refresh/current (`current_step`) — опрашивает
Apify по идущей пачке, принимает датасет, стартует следующую пачку. Состояние живёт только в БД
(radar_refreshes + дочерние radar_scrape_runs с refresh_id), в памяти процесса ничего не держим —
serverless не хранит состояние между вызовами.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import repo, scrape_service
from . import settings as rs

logger = logging.getLogger(__name__)

_TERMINAL = {"done", "error"}


class RefreshValidationError(Exception):
    """400 — нет источников или все свежие."""


class RefreshBusyError(Exception):
    """409 — обновление уже идёт."""


class RefreshLimitError(Exception):
    """429 — превышен часовой лимит обновлений."""


# ── утилиты ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "uq_radar_refresh_one_running" in text or "duplicate key" in text


# ── план обновления ──────────────────────────────────────────────────────────

def _plan(force: bool) -> dict[str, Any]:
    """Раскладка активных источников: глубокие / лёгкие / пропущенные свежие / кому нужны подписчики."""
    sources = repo.list_active_sources()
    now = _now()
    fresh_after = now - timedelta(hours=rs.MIN_REFRESH_INTERVAL_H)
    deep_before = now - timedelta(days=rs.DEEP_STALE_DAYS)
    followers_before = now - timedelta(days=rs.FOLLOWERS_TTL_DAYS)

    deep: list[str] = []
    incr: list[str] = []
    followers: list[str] = []
    skipped = 0
    for src in sources:
        name = src["username"]
        scraped = _parse_dt(src.get("last_scraped_at"))
        if scraped is not None and scraped > fresh_after and not force:
            skipped += 1
            continue
        if scraped is None or scraped < deep_before:
            deep.append(name)
        else:
            incr.append(name)
        f_updated = _parse_dt(src.get("followers_updated_at"))
        if f_updated is None or f_updated < followers_before:
            followers.append(name)
    return {
        "sources_total": len(sources),
        "deep": deep,
        "incr": incr,
        "followers": followers,
        "skipped_fresh": skipped,
    }


def estimate(force: bool = False) -> dict[str, Any]:
    """GET /refresh/estimate — сколько и почём будет обновлено сейчас."""
    plan = _plan(force)
    n_deep, n_incr, n_fol = len(plan["deep"]), len(plan["incr"]), len(plan["followers"])
    batches = (
        math.ceil(n_deep / rs.BATCH_DEEP)
        + math.ceil(n_incr / rs.BATCH_INCR)
        + math.ceil(n_fol / rs.BATCH_FOLLOWERS)
    )
    reels_est = n_deep * rs.DEEP_LIMIT + n_incr * rs.INCR_LIMIT
    reels_usd = reels_est * rs.APIFY_COST_PER_REEL
    followers_usd = n_fol * rs.APIFY_COST_PER_PROFILE
    total_usd = reels_usd + followers_usd
    return {
        "sources_total": plan["sources_total"],
        "to_refresh": n_deep + n_incr,
        "deep": n_deep,
        "incremental": n_incr,
        "batches": batches,
        "reels_est": reels_est,
        "reels_usd": round(reels_usd, 2),
        "followers_profiles": n_fol,
        "followers_usd": round(followers_usd, 2),
        "total_usd": round(total_usd, 2),
        "total_rub": float(round(total_usd * rs.USD_RUB)),
        "rate": rs.USD_RUB,
        "skipped_fresh": plan["skipped_fresh"],
    }


# ── запуск ───────────────────────────────────────────────────────────────────

def start(force: bool = False) -> int:
    """POST /refresh: проверки → строка radar_refreshes → все дочерние пачки (pending).
    Apify здесь не вызывается — первую пачку стартует первый же GET /refresh/current."""
    plan = _plan(force)
    if plan["sources_total"] == 0:
        raise RefreshValidationError("Нет источников: добавьте аккаунты во вкладке «Источники»")

    repo.close_stale_refreshes(rs.REFRESH_STALE_MIN)
    if repo.get_running_refresh() is not None:
        raise RefreshBusyError("Обновление уже идёт")
    if repo.count_refreshes_last_hour() >= rs.REFRESHES_PER_HOUR:
        raise RefreshLimitError("Слишком много обновлений за последний час, попробуйте позже")
    if not plan["deep"] and not plan["incr"]:
        raise RefreshValidationError(
            f"Все источники обновлены недавно (меньше {rs.MIN_REFRESH_INTERVAL_H} ч назад)"
        )

    today = _now().date()
    deep_since = (today - timedelta(days=rs.DEEP_DAYS)).isoformat()
    incr_since = (today - timedelta(days=rs.INCR_DAYS)).isoformat()

    # (kind, usernames, params) в порядке: глубокие → лёгкие → подписчики
    batches: list[tuple[str, list[str], dict[str, Any]]] = []
    for chunk in _chunks(plan["deep"], rs.BATCH_DEEP):
        batches.append(("reels", chunk, {
            "results_limit": rs.DEEP_LIMIT, "newer_than": deep_since, "deep": True,
        }))
    for chunk in _chunks(plan["incr"], rs.BATCH_INCR):
        batches.append(("reels", chunk, {
            "results_limit": rs.INCR_LIMIT, "newer_than": incr_since, "deep": False,
        }))
    for chunk in _chunks(plan["followers"], rs.BATCH_FOLLOWERS):
        batches.append(("followers", chunk, {}))

    est = estimate_from_plan(plan)
    try:
        refresh = repo.create_refresh(len(batches), est)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise RefreshBusyError("Обновление уже идёт") from exc
        raise

    refresh_id = refresh["id"]
    try:
        for idx, (kind, names, params) in enumerate(batches, start=1):
            repo.create_scrape_run(
                kind=kind, usernames=names, period_months=None,
                refresh_id=refresh_id, batch_idx=idx, params=params,
            )
    except Exception as exc:
        # не оставляем висеть замок на полусозданном обновлении
        repo.finalize_refresh(refresh_id, "error", 0, 0.0, [
            {"batch": None, "usernames": [], "error": f"Не удалось создать пачки: {exc}"},
        ])
        raise
    return refresh_id


def estimate_from_plan(plan: dict[str, Any]) -> float:
    reels = len(plan["deep"]) * rs.DEEP_LIMIT + len(plan["incr"]) * rs.INCR_LIMIT
    return round(reels * rs.APIFY_COST_PER_REEL + len(plan["followers"]) * rs.APIFY_COST_PER_PROFILE, 4)


# ── шаг конвейера ────────────────────────────────────────────────────────────

def _first_open(runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for run in runs:
        if run["status"] not in _TERMINAL:
            return run
    return None


def _mark_scraped(run: dict[str, Any]) -> None:
    if run.get("kind") == "reels" and run.get("status") == "done":
        repo.set_last_scraped(list(run.get("usernames") or []))


async def _advance(refresh_id: int) -> list[dict[str, Any]]:
    """Двигает конвейер: опрос идущей пачки, старт следующей. Возвращает свежий список пачек."""
    for _ in range(100):  # страховка от бесконечного цикла
        runs = repo.list_refresh_runs(refresh_id)
        run = _first_open(runs)
        if run is None:
            break

        if run["status"] == "running":
            polled = await scrape_service.poll_scrape_run(run["id"])
            if polled is not None and polled["status"] in _TERMINAL:
                # метку ставим сразу по пачке, а не в финале: если обновление не дойдёт до
                # конца, уже оплаченные источники не уйдут на повторный глубокий проход
                _mark_scraped(polled)
                continue  # пачка закрылась — в этом же вызове стартуем следующую
            break

        # pending
        if repo.claim_run_start(run["id"]):
            try:
                await scrape_service.start_refresh_batch(run)
            except Exception as exc:  # noqa: BLE001 — повторного платного запуска нет
                logger.warning("refresh %s: старт пачки %s не удался: %s", refresh_id, run["id"], exc)
                repo.mark_run_error(run["id"], str(exc))
                continue
            break

        # не захватили: либо стартует другой вызов, либо захват завис
        fresh = repo.get_scrape_run(run["id"])
        claimed_at = _parse_dt((fresh or {}).get("start_claimed_at"))
        if (
            fresh is not None
            and fresh["status"] == "pending"
            and claimed_at is not None
            and _now() - claimed_at > timedelta(seconds=rs.START_CLAIM_STALE_SEC)
        ):
            repo.mark_run_error(run["id"], "Старт пачки завис и не завершился")
            continue
        break
    return repo.list_refresh_runs(refresh_id)


def _collect(runs: list[dict[str, Any]]) -> dict[str, Any]:
    reels_runs = [r for r in runs if r["kind"] == "reels"]
    done_reels = [r for r in reels_runs if r["status"] == "done"]
    errors = [
        {"batch": r.get("batch_idx"), "usernames": list(r.get("usernames") or []),
         "error": r.get("error") or "Ошибка"}
        for r in runs if r["status"] == "error"
    ]
    return {
        "reels_total": len(reels_runs),
        "reels_terminal": sum(1 for r in reels_runs if r["status"] in _TERMINAL),
        "reels_saved": sum(int(r.get("results_count") or 0) for r in done_reels),
        "cost_usd": round(sum(float(r.get("cost_estimate_usd") or 0) for r in runs), 4),
        "errors": errors,
        "terminal": sum(1 for r in runs if r["status"] in _TERMINAL),
    }


def _finalize(refresh: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    info = _collect(runs)
    for run in runs:
        _mark_scraped(run)
    status = "done" if any(r["status"] == "done" for r in runs) else "error"
    repo.finalize_refresh(refresh["id"], status, info["reels_saved"], info["cost_usd"], info["errors"])


def _view(refresh: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    info = _collect(runs)
    status = refresh["status"]
    stage: Optional[str] = None
    label = ""
    if status == "running":
        opened = _first_open(runs)
        if opened is None:
            stage, label = "finishing", "завершаем"
        elif opened["kind"] == "followers":
            stage, label = "followers", "подписчики"
        else:
            k = min(info["reels_terminal"] + 1, max(info["reels_total"], 1))
            stage, label = "reels", f"рилсы {k}/{info['reels_total']} пачки"
        reels_saved, cost, errors = info["reels_saved"], info["cost_usd"], info["errors"]
    else:
        label = "готово" if status == "done" else "ошибка"
        reels_saved = refresh.get("reels_saved") or 0
        cost = float(refresh.get("cost_usd") or 0)
        errors = refresh.get("errors") or []
    return {
        "id": refresh["id"],
        "status": status,
        "stage": stage,
        "stage_label": label,
        "batch_done": info["terminal"] if status == "running" else refresh.get("total_batches", 0),
        "batch_total": refresh.get("total_batches", len(runs)),
        "reels_saved": reels_saved,
        "cost_usd": round(cost, 4),
        "cost_rub": float(round(cost * rs.USD_RUB)),
        "errors": errors,
        "started_at": refresh.get("created_at"),
        "finished_at": refresh.get("finished_at"),
    }


async def current_step() -> dict[str, Any]:
    """GET /refresh/current: двигает идущее обновление на шаг и отдаёт состояние.
    Нет идущего — отдаёт последнее завершённое (без продвижения) или {"status": "idle"}."""
    # Зависшие здесь НЕ закрываем: вернувшись через час, владелец должен продолжить то же
    # обновление (датасеты Apify живут днями), а не потерять оплаченные пачки. Страховочное
    # закрытие — только в start().
    refresh = repo.get_running_refresh()
    if refresh is None:
        latest = repo.get_latest_refresh()
        if latest is None:
            return {"status": "idle"}
        return _view(latest, repo.list_refresh_runs(latest["id"]))

    runs = await _advance(refresh["id"])
    if _first_open(runs) is None:
        _finalize(refresh, runs)
        refresh = repo.get_latest_refresh() or refresh
        return _view(refresh, runs)
    return _view(refresh, runs)
