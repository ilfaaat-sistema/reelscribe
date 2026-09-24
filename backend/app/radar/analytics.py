"""Аналитика: сводка по конкурентам, корзины по просмотрам, лента рилсов.

Перенос Радар/backend/services/analytics.py с sqlite на Supabase (через app.radar.repo).
Формулы (медианы, залётность/reach, likes_rate, comments_rate) — те же, что в оригинале.
"""
from __future__ import annotations

import statistics
from typing import Any, Optional

from . import repo

# Корзины по просмотрам: [min, max) — max=None означает «без верхней границы».
_VIEW_BUCKETS: list[tuple[int, float, str]] = [
    (0, 10_000, "< 10к"),
    (10_000, 50_000, "10к – 50к"),
    (50_000, 100_000, "50к – 100к"),
    (100_000, 200_000, "100к – 200к"),
    (200_000, float("inf"), "200к +"),
]

_SORT_COLUMNS = {"views", "likes", "comments"}


def _median(values: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _parse_usernames(usernames: Optional[str]) -> list[str]:
    if not usernames:
        return []
    return [u.lstrip("@").strip() for u in usernames.split(",") if u.strip()]


def _bucket_counts(views: list[int]) -> list[dict[str, Any]]:
    result = []
    for lo, hi, label in _VIEW_BUCKETS:
        count = sum(1 for v in views if lo <= v < hi)
        result.append({
            "label": label,
            "min": lo,
            "max": None if hi == float("inf") else hi,
            "count": count,
        })
    return result


def _account_stats(reels: list[dict[str, Any]], filter_names: list[str]) -> list[dict[str, Any]]:
    by_username: dict[str, list[dict[str, Any]]] = {}
    for r in reels:
        by_username.setdefault(r["username"], []).append(r)

    usernames = filter_names or list(by_username.keys())
    competitors = {c["username"]: c for c in repo.list_competitors()}

    result = []
    for uname in usernames:
        rows = by_username.get(uname, [])
        views = [r.get("views") or 0 for r in rows]
        likes = [r.get("likes") or 0 for r in rows]
        comments = [r.get("comments") or 0 for r in rows]

        med_views = _median(views)
        med_likes = _median(likes)
        med_comments = _median(comments)

        comp = competitors.get(uname)
        followers = comp.get("followers") if comp else None

        reach = round(med_views / followers, 4) if (med_views and followers) else None
        likes_rate = round(med_likes / followers, 4) if (med_likes and followers) else None
        comments_rate = round(med_comments / followers, 4) if (med_comments and followers) else None

        result.append({
            "username": uname,
            "followers": followers,
            "followers_updated_at": comp.get("followers_updated_at") if comp else None,
            "reels_count": len(rows),
            "median_views": med_views,
            "median_likes": med_likes,
            "median_comments": med_comments,
            "reach": reach,
            "likes_rate": likes_rate,
            "comments_rate": comments_rate,
        })

    result.sort(key=lambda x: x["reach"] or 0, reverse=True)
    return result


def get_summary(usernames: Optional[str]) -> dict[str, Any]:
    names = _parse_usernames(usernames)
    reels = repo.list_reels_for_summary(names)
    accounts = _account_stats(reels, names)

    if not reels:
        return {
            "total_reels": 0,
            "median_views": None,
            "best_account": None,
            "buckets": _bucket_counts([]),
            "accounts": accounts,
        }

    # best_account — по МЕДИАНЕ просмотров (не по reach, которым отсортирован accounts — это
    # разные метрики, ровно как в исходном Радар/backend/services/analytics.py::get_summary).
    by_username: dict[str, list[int]] = {}
    for r in reels:
        by_username.setdefault(r["username"], []).append(r.get("views") or 0)

    best_account = None
    best_median = -1.0
    for uname, views in by_username.items():
        med = _median(views) or 0
        if med > best_median:
            best_median = med
            best_account = uname

    all_views = [r.get("views") or 0 for r in reels]
    return {
        "total_reels": len(reels),
        "median_views": _median(all_views),
        "best_account": best_account,
        "buckets": _bucket_counts(all_views),
        "accounts": accounts,
    }


def get_reels(
    usernames: Optional[str] = None,
    sort: str = "views",
    order: str = "desc",
    min_views: Optional[int] = None,
    max_views: Optional[int] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    names = _parse_usernames(usernames)
    sort_col = sort if sort in _SORT_COLUMNS else "views"
    desc = order.lower() != "asc"

    rows = repo.list_reels(
        usernames=names, min_views=min_views, max_views=max_views,
        sort_col=sort_col, desc=desc, limit=limit,
    )
    ids = [r["id"] for r in rows]
    analyses = repo.list_analyses_status(ids)
    for r in rows:
        a = analyses.get(r["id"])
        r["analysis_status"] = a["status"] if a else None
        r["analysis_mode"] = a["mode"] if a else None
    return rows
