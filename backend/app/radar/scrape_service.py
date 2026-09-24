"""Сбор рилсов/подписчиков конкурентов: валидация, лимиты, старт прогона Apify, приём датасета.

Лимиты — «защита кошелька» (эндпоинты публичные, auth в проекте нет, см. docs/specs/08-radar.md,
раздел «Архитектура» → «Лимиты»). Считаются ПО БАЗЕ (radar_scrape_runs.created_at), не в памяти
процесса — serverless-функция Vercel не держит состояние между вызовами.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.pipeline.apify_client import ApifyExhaustedError

from . import apify_runs, repo
from . import settings as radar_settings

logger = logging.getLogger(__name__)


class RadarValidationError(Exception):
    """400 — пустой/некорректный ввод или превышены статические лимиты (число ников, период)."""


class RadarHourlyLimitError(Exception):
    """429 — превышен часовой лимит прогонов сбора (SCRAPES_PER_HOUR)."""


class RadarApifyError(Exception):
    """502 — Apify не ответил или вернул ошибку при старте прогона."""


_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_FAIL = {"FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


def normalize_usernames(raw: list[str]) -> list[str]:
    """Обрезает '@', пробелы, убирает пустые и дубли, сохраняя порядок первого вхождения."""
    seen: dict[str, None] = {}
    for u in raw:
        name = (u or "").lstrip("@").strip()
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def estimate_cost(usernames: list[str], period_months: int) -> dict[str, Any]:
    n_reels = len(usernames) * radar_settings.REELS_PER_MONTH_ESTIMATE * max(period_months, 0)
    return {
        "estimate_usd": round(n_reels * radar_settings.APIFY_COST_PER_REEL, 2),
        "n_reels": n_reels,
    }


def _cutoff_date_str(period_months: int) -> str:
    now = datetime.now(timezone.utc)
    month = now.month - period_months
    year = now.year
    while month <= 0:
        month += 12
        year -= 1
    return now.replace(year=year, month=month, day=min(now.day, 28)).strftime("%Y-%m-%d")


async def start_reel_scrape(usernames: list[str], period_months: int) -> int:
    """Валидирует вход, проверяет часовой лимит, запускает apify~instagram-reel-scraper
    асинхронно и заводит строку radar_scrape_runs(kind='reels'). Возвращает run_id."""
    names = normalize_usernames(usernames)
    if not names:
        raise RadarValidationError("Список ников пуст")
    if len(names) > radar_settings.MAX_USERNAMES:
        raise RadarValidationError(f"Не больше {radar_settings.MAX_USERNAMES} ников за раз")
    if not (1 <= period_months <= radar_settings.MAX_PERIOD_MONTHS):
        raise RadarValidationError(f"Период — от 1 до {radar_settings.MAX_PERIOD_MONTHS} месяцев")

    if repo.count_runs_last_hour() >= radar_settings.SCRAPES_PER_HOUR:
        raise RadarHourlyLimitError("Слишком много прогонов сбора за последний час, попробуйте позже")

    run = repo.create_scrape_run(kind="reels", usernames=names, period_months=period_months)
    run_id = run["id"]

    results_limit = radar_settings.REELS_PER_MONTH_ESTIMATE * period_months
    run_input = {
        "username": names,
        "resultsLimit": results_limit,
        "onlyPostsNewerThan": _cutoff_date_str(period_months),
    }
    try:
        started = await apify_runs.start_run(radar_settings.REEL_ACTOR, run_input)
    except (ApifyExhaustedError, apify_runs.ApifyRunError) as exc:
        repo.finish_scrape_run_error(run_id, str(exc))
        raise RadarApifyError(str(exc)) from exc

    repo.set_run_started(run_id, started["run_id"], started["dataset_id"], started["token_ref"])
    return run_id


async def start_followers_scrape(usernames: Optional[list[str]]) -> int:
    """Обновление подписчиков — kind='followers', актор PROFILE_ACTOR. Без явных usernames
    берёт ВСЕ аккаунты, уже встречавшиеся в radar_reels (как было в исходном Радаре)."""
    names = normalize_usernames(usernames) if usernames else repo.list_distinct_reel_usernames()
    if not names:
        raise RadarValidationError("Нет аккаунтов для обновления подписчиков")

    if repo.count_runs_last_hour() >= radar_settings.SCRAPES_PER_HOUR:
        raise RadarHourlyLimitError("Слишком много прогонов сбора за последний час, попробуйте позже")

    run = repo.create_scrape_run(kind="followers", usernames=names, period_months=None)
    run_id = run["id"]

    try:
        started = await apify_runs.start_run(radar_settings.PROFILE_ACTOR, {"usernames": names})
    except (ApifyExhaustedError, apify_runs.ApifyRunError) as exc:
        repo.finish_scrape_run_error(run_id, str(exc))
        raise RadarApifyError(str(exc)) from exc

    repo.set_run_started(run_id, started["run_id"], started["dataset_id"], started["token_ref"])
    return run_id


def _map_reel(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Apify item → строка radar_reels. Без shortCode строка не идентифицируема — пропускаем
    (маппинг сверен с Радар/backend/services/apify_scraper.py::_map_reel)."""
    reel_id = item.get("shortCode")
    if not reel_id:
        return None

    music = None
    music_info = item.get("musicInfo") or {}
    if music_info:
        artist = music_info.get("artistName") or ""
        song = music_info.get("songName") or ""
        music = f"{artist} — {song}".strip(" —") or None

    hashtags = item.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = []

    timestamp = item.get("timestamp")
    posted_at = None
    if isinstance(timestamp, (int, float)):
        posted_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    elif isinstance(timestamp, str) and timestamp:
        posted_at = timestamp

    return {
        "id": reel_id,
        "username": item.get("ownerUsername") or item.get("username"),
        "url": item.get("url"),
        "caption": item.get("caption"),
        "hashtags": hashtags,
        "music": music,
        "views": item.get("videoViewCount") or item.get("videoPlayCount") or 0,
        "likes": item.get("likesCount") or 0,
        "comments": item.get("commentsCount") or 0,
        "duration_sec": item.get("videoDuration"),
        "posted_at": posted_at,
        "video_url": item.get("videoUrl"),
    }


async def poll_scrape_run(run_id: int) -> Optional[dict[str, Any]]:
    """GET /scrape/{run_id}: опрашивает Apify (если прогон ещё running) и принимает датасет
    при SUCCEEDED. Идемпотентно — повторный вызов после done/error просто отдаёт текущую
    строку, не трогая Apify и не пересчитывая results_count/cost (репо.finish_* — условный
    update по status='running')."""
    run = repo.get_scrape_run(run_id)
    if run is None:
        return None
    if run["status"] != "running":
        return run

    try:
        status = await apify_runs.get_run(run["apify_run_id"], run["apify_token_ref"])
    except apify_runs.ApifyRunError as exc:
        repo.finish_scrape_run_error(run_id, str(exc))
        return repo.get_scrape_run(run_id)

    apify_status = status.get("status")
    if apify_status in _TERMINAL_FAIL:
        repo.finish_scrape_run_error(run_id, f"Прогон Apify завершился статусом {apify_status}")
        return repo.get_scrape_run(run_id)
    if apify_status not in _TERMINAL_OK:
        return run  # ещё выполняется — статус в базе остаётся running

    try:
        items = await apify_runs.get_items(run["apify_dataset_id"], run["apify_token_ref"])
    except apify_runs.ApifyRunError as exc:
        repo.finish_scrape_run_error(run_id, str(exc))
        return repo.get_scrape_run(run_id)

    if run["kind"] == "followers":
        saved = 0
        for item in items:
            username = item.get("username") or (item.get("inputUrl") or "").rstrip("/").split("/")[-1]
            followers = item.get("followersCount")
            if username and followers is not None:
                repo.upsert_competitor_followers(username, followers)
                saved += 1
        repo.finish_scrape_run_done(run_id, saved, 0.0)
        return repo.get_scrape_run(run_id)

    reels = [r for r in (_map_reel(item) for item in items) if r]
    repo.ensure_competitors([r["username"] for r in reels if r.get("username")])
    repo.bulk_upsert_reels(reels, scrape_run_id=run_id)
    cost = round(len(reels) * radar_settings.APIFY_COST_PER_REEL, 4)
    repo.finish_scrape_run_done(run_id, len(reels), cost)
    return repo.get_scrape_run(run_id)
