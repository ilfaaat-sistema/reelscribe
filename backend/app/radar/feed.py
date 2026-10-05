"""Расчёт ленты и источников разведки Радара (docs/specs/10-radar-intel.md, «Расчёт»).

Чистые функции без обращения к БД: вход — списки dict, «сейчас» передаётся параметром.
Тонкие сборщики build_feed/build_sources в конце берут данные из repo_intel.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import intel_settings as cfg

SORT_KEYS = ("viral", "views", "speed", "reach", "posted_at", "followers", "likes", "comments")
_SECONDS_PER_DAY = 86400.0


# ── мелкие помощники ─────────────────────────────────────────────────────────

def parse_dt(value: Any) -> Optional[datetime]:
    """ISO-строка (в т.ч. с 'Z') → aware datetime; мусор и None → None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_days(posted_at: Any, now: datetime) -> Optional[float]:
    dt = parse_dt(posted_at)
    if dt is None:
        return None
    return (now - dt).total_seconds() / _SECONDS_PER_DAY


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean_median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    m = statistics.median(values)
    return int(m) if float(m).is_integer() else round(m, 1)


def hidden_to_zero(v: Any) -> int:
    """Скрытые лайки/комментарии приходят как -1 (и None) → 0."""
    n = _num(v)
    return int(n) if n is not None and n > 0 else 0


def viral_level(ratio: Optional[float]) -> Optional[int]:
    """Уровень бейджа: 3 / 5 / 10 или None, если ниже первого порога."""
    if ratio is None:
        return None
    level = None
    for lv in cfg.VIRAL_LEVELS:
        if ratio >= lv:
            level = int(lv)
    return level


# ── норма и залётность ───────────────────────────────────────────────────────

def compute_norm(reels: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Норма одного аккаунта: медиана views по рилсам с возрастом MATURE_DAYS..NORM_WINDOW_DAYS,
    не более NORM_MAX_REELS самых свежих. Сам рилс из медианы не исключается."""
    pool: list[tuple[float, float]] = []  # (age, views)
    for r in reels:
        views = _num(r.get("views"))
        age = age_days(r.get("posted_at"), now)
        if views is None or age is None:
            continue
        if cfg.MATURE_DAYS <= age <= cfg.NORM_WINDOW_DAYS:
            pool.append((age, views))
    pool.sort(key=lambda t: t[0])  # самые свежие первыми
    pool = pool[:cfg.NORM_MAX_REELS]
    n = len(pool)
    return {
        "norm_views": _clean_median([v for _, v in pool]),
        "norm_n": n,
        "norm_reliable": n >= cfg.NORM_RELIABLE_N,
    }


def compute_norms(reels: list[dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    by_user: dict[str, list[dict[str, Any]]] = {}
    for r in reels:
        by_user.setdefault(r["username"], []).append(r)
    return {u: compute_norm(rs, now) for u, rs in by_user.items()}


def viral_ratio(views: Any, norm: dict[str, Any]) -> Optional[float]:
    v = _num(views)
    nv = norm.get("norm_views")
    if v is None or not nv or norm.get("norm_n", 0) < cfg.NORM_MIN_N:
        return None
    return round(v / nv, 2)


def is_viral(ratio: Optional[float], norm: dict[str, Any]) -> bool:
    return bool(norm.get("norm_reliable")) and ratio is not None and ratio >= cfg.VIRAL_RATIO_THRESHOLD


def compute_speed(views: Any, age: Optional[float]) -> Optional[float]:
    v = _num(views)
    if v is None or age is None:
        return None
    return v / max(1.0, age)


def compute_reach(views: Any, followers: Any) -> Optional[float]:
    v, f = _num(views), _num(followers)
    if v is None or not f:
        return None
    return v / f


# ── «набирает» ───────────────────────────────────────────────────────────────

def percentile_speed(speeds: list[float]) -> Optional[float]:
    """P70: элемент с индексом int(len × 0.7) в отсортированном по возрастанию списке."""
    if not speeds:
        return None
    s = sorted(speeds)
    return s[min(int(len(s) * cfg.RISING_PERCENTILE), len(s) - 1)]


def _snapshot_pair(snapshots: list[dict[str, Any]]) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """Последний снимок v2 и ближайший более ранний v1 с разрывом ≥ RISING_MIN_GAP_HOURS."""
    pts = []
    for s in snapshots:
        t, v = parse_dt(s.get("taken_at")), _num(s.get("views"))
        if t is not None and v is not None:
            pts.append((t, v))
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])
    t2, v2 = pts[-1]
    gap = timedelta(hours=cfg.RISING_MIN_GAP_HOURS)
    for t1, v1 in reversed(pts[:-1]):
        if t2 - t1 >= gap:
            return ({"t": t1, "v": v1}, {"t": t2, "v": v2})
    return None


def compute_rising(
    age: Optional[float],
    speed: Optional[float],
    snapshots: list[dict[str, Any]],
    p70: Optional[float],
) -> tuple[bool, Optional[str]]:
    """(rising, rising_mode). Ветка «history» — по паре снимков, иначе «age» — по возрасту и P70."""
    if age is None or speed is None:
        return False, None
    pair = _snapshot_pair(snapshots)
    if pair is not None:
        first, last = pair
        if last["v"] >= first["v"]:
            days = (last["t"] - first["t"]).total_seconds() / _SECONDS_PER_DAY
            recent = (last["v"] - first["v"]) / days
            if recent >= cfg.RISING_RATIO * speed and age <= cfg.RISING_MAX_AGE_DAYS:
                return True, "history"
            return False, None
    if age <= cfg.RISING_NOHISTORY_MAX_AGE_DAYS and p70 is not None and speed >= p70:
        return True, "age"
    return False, None


# ── сортировка, фильтры, пагинация ───────────────────────────────────────────

def normalize_sort(sort: Optional[str]) -> str:
    return sort if sort in SORT_KEYS else "viral"


def _sort_value(row: dict[str, Any], key: str) -> Optional[float]:
    if key == "posted_at":
        dt = parse_dt(row.get("posted_at"))
        return dt.timestamp() if dt else None
    return _num(row.get(key if key != "viral" else "viral_ratio"))


def sort_rows(rows: list[dict[str, Any]], sort: str, order: str) -> list[dict[str, Any]]:
    """Сортировка с null всегда в конце (при любом направлении); при равенстве — по id."""
    key = normalize_sort(sort)
    desc = order != "asc"
    have = [r for r in rows if _sort_value(r, key) is not None]
    nulls = [r for r in rows if _sort_value(r, key) is None]
    have.sort(key=lambda r: str(r.get("id")))
    have.sort(key=lambda r: _sort_value(r, key), reverse=desc)  # type: ignore[arg-type,return-value]
    nulls.sort(key=lambda r: str(r.get("id")))
    return have + nulls


def paginate(rows: list[Any], limit: int, offset: int) -> list[Any]:
    return rows[max(offset, 0):max(offset, 0) + max(limit, 0)]


def _base_row(r: dict[str, Any], now: datetime, norm: dict[str, Any], followers: Any) -> dict[str, Any]:
    age = age_days(r.get("posted_at"), now)
    ratio = viral_ratio(r.get("views"), norm)
    return {
        "id": r["id"],
        "username": r["username"],
        "posted_at": r.get("posted_at"),
        "age": age,
        "views": r.get("views"),
        "likes": hidden_to_zero(r.get("likes")),
        "comments": hidden_to_zero(r.get("comments")),
        "followers": followers,
        "norm_views": norm.get("norm_views"),
        "norm_n": norm.get("norm_n", 0),
        "norm_reliable": bool(norm.get("norm_reliable")),
        "viral_ratio": ratio,
        "is_viral": is_viral(ratio, norm),
        "speed": compute_speed(r.get("views"), age),
        "reach": compute_reach(r.get("views"), followers),
    }


def compute_rows(
    reels: list[dict[str, Any]],
    followers_by_user: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], Optional[float]]:
    """Все рилсы активных источников с расчётом (без rising) и P70 скоростей ВСЕХ рилсов."""
    norms = compute_norms(reels, now)
    rows = [_base_row(r, now, norms[r["username"]], followers_by_user.get(r["username"])) for r in reels]
    p70 = percentile_speed([row["speed"] for row in rows if row["speed"] is not None])
    return rows, p70


def filter_period(rows: list[dict[str, Any]], period: int, now: datetime) -> list[dict[str, Any]]:
    if not period:
        return rows
    border = now - timedelta(days=period)
    out = []
    for r in rows:
        dt = parse_dt(r.get("posted_at"))
        if dt is not None and dt >= border:
            out.append(r)
    return out


def count_accounts(rows: list[dict[str, Any]], sources: list[str]) -> list[dict[str, Any]]:
    counts = {u: 0 for u in sources}
    for r in rows:
        if r["username"] in counts:
            counts[r["username"]] += 1
    items = [{"username": u, "count": c} for u, c in counts.items()]
    items.sort(key=lambda a: (-a["count"], a["username"]))
    return items


def _round(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(v, nd)


def format_item(
    row: dict[str, Any],
    full: dict[str, Any],
    snapshots: list[dict[str, Any]],
    p70: Optional[float],
    analysis: Optional[dict[str, Any]],
) -> dict[str, Any]:
    rising, mode = compute_rising(row["age"], row["speed"], snapshots, p70)
    return {
        "id": row["id"],
        "url": full.get("url"),
        "username": row["username"],
        "caption": full.get("caption"),
        "posted_at": row["posted_at"],
        "age_days": _round(row["age"], 1),
        "duration_sec": full.get("duration_sec"),
        "views": row["views"],
        "likes": row["likes"],
        "comments": row["comments"],
        "followers": row["followers"],
        "norm_views": row["norm_views"],
        "norm_n": row["norm_n"],
        "norm_reliable": row["norm_reliable"],
        "viral_ratio": row["viral_ratio"],
        "is_viral": row["is_viral"],
        "speed": _round(row["speed"], 1),
        "reach": _round(row["reach"], 2),
        "rising": rising,
        "rising_mode": mode,
        "analysis_status": analysis["status"] if analysis else None,
        "analysis_mode": analysis["mode"] if analysis else None,
    }


# ── сборщики ─────────────────────────────────────────────────────────────────

def build_feed(
    period: int = 30,
    usernames: Optional[list[str]] = None,
    only_viral: bool = False,
    sort: str = "viral",
    order: str = "desc",
    limit: int = 60,
    offset: int = 0,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    from . import repo_intel  # поздний импорт: чистые функции выше не зависят от БД

    now = now or datetime.now(timezone.utc)
    sources = repo_intel.list_active_sources()
    names = [s["username"] for s in sources]
    followers = {s["username"]: s.get("followers") for s in sources}
    reels = repo_intel.list_light_reels(names)
    rows, p70 = compute_rows(reels, followers, now)

    in_period = filter_period(rows, period, now)
    accounts = count_accounts(in_period, names)  # до фильтров usernames / only_viral

    filtered = in_period
    if usernames:
        wanted = {u.lower() for u in usernames}
        filtered = [r for r in filtered if r["username"] in wanted]
    if only_viral:
        filtered = [r for r in filtered if r["is_viral"]]

    ordered = sort_rows(filtered, sort, order)
    page = paginate(ordered, limit, offset)

    ids = [r["id"] for r in page]
    fulls = repo_intel.get_full_reels(ids)
    young = [r["id"] for r in page if r["age"] is not None and r["age"] <= cfg.RISING_MAX_AGE_DAYS]
    snaps = repo_intel.list_snapshots(young)
    analyses = repo_intel.list_analyses_status(ids)
    items = [
        format_item(r, fulls.get(r["id"], {}), snaps.get(r["id"], []), p70, analyses.get(r["id"]))
        for r in page
    ]
    return {
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "viral_threshold": cfg.VIRAL_RATIO_THRESHOLD,
        "last_refresh_at": repo_intel.get_last_refresh_at(),
        "accounts": accounts,
        "items": items,
    }


def source_stats(
    source: dict[str, Any],
    reels: list[dict[str, Any]],
    now: datetime,
    deep_stale_days: int,
) -> dict[str, Any]:
    norm = compute_norm(reels, now)
    viral_count = 0
    last_dt: Optional[datetime] = None
    for r in reels:
        ratio = viral_ratio(r.get("views"), norm)
        if is_viral(ratio, norm):
            viral_count += 1
        dt = parse_dt(r.get("posted_at"))
        if dt is not None and (last_dt is None or dt > last_dt):
            last_dt = dt
    scraped = parse_dt(source.get("last_scraped_at"))
    needs_deep = scraped is None or (now - scraped) > timedelta(days=deep_stale_days)
    return {
        "username": source["username"],
        "full_name": source.get("full_name"),
        "followers": source.get("followers"),
        "followers_updated_at": source.get("followers_updated_at"),
        "median_views": norm["norm_views"],
        "norm_n": norm["norm_n"],
        "norm_reliable": norm["norm_reliable"],
        "last_reel_at": last_dt.isoformat() if last_dt else None,
        "reels_count": len(reels),
        "viral_count": viral_count,
        "last_scraped_at": source.get("last_scraped_at"),
        "added_at": source.get("source_added_at"),
        "needs_deep": needs_deep,
    }


def build_sources(now: Optional[datetime] = None) -> dict[str, Any]:
    from . import repo_intel
    from . import settings as radar_settings

    now = now or datetime.now(timezone.utc)
    deep_stale = getattr(radar_settings, "DEEP_STALE_DAYS", 14)  # константу заводит модуль «Обновление»
    active = repo_intel.list_active_sources()
    reels = repo_intel.list_light_reels([s["username"] for s in active])
    by_user: dict[str, list[dict[str, Any]]] = {}
    for r in reels:
        by_user.setdefault(r["username"], []).append(r)
    return {
        "active": [source_stats(s, by_user.get(s["username"], []), now, deep_stale) for s in active],
        "excluded": [
            {"username": s["username"], "excluded_at": s.get("excluded_at"), "followers": s.get("followers")}
            for s in repo_intel.list_excluded_sources()
        ],
        "max_sources": cfg.MAX_SOURCES,
    }
