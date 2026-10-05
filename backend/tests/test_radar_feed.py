"""Тесты расчёта ленты разведки (app/radar/feed.py, repo_intel.py) — ТЗ 10.

Чистые функции проверяются на списках dict; сборщики и постраничный сбор — на фейковом db.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from app.radar import feed, repo_intel

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def reel(i: Any, user: str = "a", views: Optional[int] = 1000, days: Optional[float] = 10,
         likes: int = 1, comments: int = 1) -> dict[str, Any]:
    return {"id": f"r{i}", "username": user, "views": views, "likes": likes, "comments": comments,
            "posted_at": None if days is None else iso(days)}


def mature(n: int, views: int = 1000, user: str = "a") -> list[dict[str, Any]]:
    return [reel(f"{user}m{i}", user, views, 10 + i) for i in range(n)]


# ── норма ───────────────────────────────────────────────────────────────────

def test_norm_n_below_min_gives_null_ratio():
    rows, _ = feed.compute_rows(mature(4, 1000), {}, NOW)
    assert all(r["viral_ratio"] is None and r["norm_n"] == 4 for r in rows)
    assert all(r["is_viral"] is False for r in rows)


def test_norm_seven_number_without_viral_badge():
    reels = mature(6, 1000) + [reel("x", views=5000, days=5)]
    rows, _ = feed.compute_rows(reels, {}, NOW)
    x = next(r for r in rows if r["id"] == "rx")
    assert x["norm_n"] == 7 and x["norm_reliable"] is False
    assert x["viral_ratio"] == 5.0 and x["is_viral"] is False  # число есть, залётным не считается


def test_norm_twelve_full_calc():
    reels = mature(11, 1000) + [reel("x", views=4000, days=5)]
    rows, _ = feed.compute_rows(reels, {}, NOW)
    x = next(r for r in rows if r["id"] == "rx")
    assert x["norm_n"] == 12 and x["norm_reliable"] is True
    assert x["norm_views"] == 1000 and x["viral_ratio"] == 4.0 and x["is_viral"] is True


def test_fresh_and_old_excluded_from_norm():
    reels = [reel(1, days=2.9, views=10**6), reel(2, days=91, views=10**6),
             reel(3, days=3, views=100), reel(4, days=90, views=300), reel(5, views=None, days=10),
             reel(6, views=500, days=None)]
    norm = feed.compute_norm(reels, NOW)
    assert norm["norm_n"] == 2 and norm["norm_views"] == 200


def test_norm_takes_at_most_60_freshest():
    old = [reel(f"o{i}", views=10, days=60 + i * 0.1) for i in range(30)]
    fresh = [reel(f"f{i}", views=1000, days=5 + i * 0.1) for i in range(60)]
    norm = feed.compute_norm(old + fresh, NOW)
    assert norm["norm_n"] == 60 and norm["norm_views"] == 1000


def test_norm_zero_gives_null_ratio():
    rows, _ = feed.compute_rows(mature(12, 0) + [reel("x", views=50, days=4)], {}, NOW)
    assert next(r for r in rows if r["id"] == "rx")["viral_ratio"] is None


# ── залётность и уровни ─────────────────────────────────────────────────────

def test_is_viral_boundary_three():
    norm = {"norm_views": 100, "norm_n": 12, "norm_reliable": True}
    r = feed.viral_ratio(300, norm)
    assert r == 3.0 and feed.is_viral(r, norm) is True
    r2 = feed.viral_ratio(299, norm)
    assert r2 == 2.99 and feed.is_viral(r2, norm) is False


def test_viral_levels():
    assert feed.viral_level(None) is None
    assert feed.viral_level(2.99) is None
    assert feed.viral_level(3.0) == 3
    assert feed.viral_level(4.99) == 3
    assert feed.viral_level(5.0) == 5
    assert feed.viral_level(10.0) == 10


def test_speed_for_reel_younger_than_day_uses_divisor_one():
    assert feed.compute_speed(1000, 0.25) == 1000
    assert feed.compute_speed(1000, 2.0) == 500
    assert feed.compute_speed(None, 2.0) is None


def test_reach_null_without_followers():
    assert feed.compute_reach(1000, None) is None
    assert feed.compute_reach(1000, 0) is None
    assert feed.compute_reach(1000, 500) == 2.0


def test_hidden_likes_become_zero():
    rows, _ = feed.compute_rows([reel(1, likes=-1, comments=-1)], {}, NOW)
    assert rows[0]["likes"] == 0 and rows[0]["comments"] == 0


# ── «набирает» ──────────────────────────────────────────────────────────────

def snap(days_ago: float, views: int) -> dict[str, Any]:
    return {"taken_at": iso(days_ago), "views": views}


def test_rising_history_pair_with_gap():
    # возраст 4 дня, 4000 просмотров → speed 1000/день; за сутки +2000 → 2000 >= 1.2*1000
    rising, mode = feed.compute_rising(4.0, 1000.0, [snap(2, 2000), snap(1, 4000)], p70=10**9)
    assert (rising, mode) == (True, "history")


def test_rising_history_not_fast_enough_is_not_rising_without_age_fallback():
    rising, mode = feed.compute_rising(4.0, 1000.0, [snap(2, 3500), snap(1, 4000)], p70=0)
    assert (rising, mode) == (False, None)  # recent=500 < 1200, но ветка age не включается


def test_rising_pair_gap_below_20h_ignored():
    snaps = [{"taken_at": iso(1.0), "views": 100}, {"taken_at": (NOW - timedelta(hours=1)).isoformat(), "views": 9999}]
    # разрыв 23 ч — ок; а теперь 19 ч — пары нет → ветка age
    ok = feed.compute_rising(4.0, 1000.0, snaps, p70=10**9)
    assert ok == (True, "history")
    short = [{"taken_at": (NOW - timedelta(hours=20)).isoformat(), "views": 100},
             {"taken_at": (NOW - timedelta(hours=1)).isoformat(), "views": 9999}]
    assert feed.compute_rising(4.0, 1000.0, short, p70=10**9) == (False, None)
    assert feed.compute_rising(4.0, 1000.0, short, p70=500.0) == (True, "age")


def test_rising_history_age_over_14_days_is_false():
    rising, mode = feed.compute_rising(15.0, 100.0, [snap(3, 1000), snap(1, 9000)], p70=0)
    assert (rising, mode) == (False, None)


def test_rising_negative_delta_falls_to_age_branch():
    rising, mode = feed.compute_rising(3.0, 500.0, [snap(3, 9000), snap(1, 8000)], p70=400.0)
    assert (rising, mode) == (True, "age")


def test_rising_age_branch_p70_and_age_limit():
    assert feed.compute_rising(5.0, 500.0, [], p70=500.0) == (True, "age")
    assert feed.compute_rising(5.0, 499.0, [], p70=500.0) == (False, None)
    assert feed.compute_rising(7.5, 900.0, [], p70=500.0) == (False, None)


def test_percentile_index():
    assert feed.percentile_speed([]) is None
    assert feed.percentile_speed([5, 1, 3, 2, 4]) == 4  # int(5*0.7)=3 -> отсортированный [1..5][3]
    assert feed.percentile_speed([7]) == 7
    assert feed.percentile_speed(list(range(10))) == 7


def test_p70_uses_all_reels_not_filtered():
    reels = [reel(i, views=(i + 1) * 100, days=5) for i in range(10)]
    _, p70 = feed.compute_rows(reels, {}, NOW)
    assert p70 == pytest.approx(160.0)  # views 100..1000 / 5 дней -> индекс 7 = 800/5


# ── сортировка, фильтры, пагинация ──────────────────────────────────────────

def _rows_for_sort() -> list[dict[str, Any]]:
    return [
        {"id": "a", "viral_ratio": 2.0, "views": 10, "speed": 5.0, "reach": 1.0,
         "posted_at": iso(3), "followers": 100, "likes": 1, "comments": 9},
        {"id": "b", "viral_ratio": None, "views": 30, "speed": None, "reach": None,
         "posted_at": None, "followers": None, "likes": 3, "comments": 7},
        {"id": "c", "viral_ratio": 5.0, "views": 20, "speed": 9.0, "reach": 3.0,
         "posted_at": iso(1), "followers": 300, "likes": 2, "comments": 8},
    ]


@pytest.mark.parametrize("key,asc_ids,desc_ids", [
    ("viral", ["a", "c", "b"], ["c", "a", "b"]),
    ("views", ["a", "c", "b"], ["b", "c", "a"]),
    ("speed", ["a", "c", "b"], ["c", "a", "b"]),
    ("reach", ["a", "c", "b"], ["c", "a", "b"]),
    ("posted_at", ["a", "c", "b"], ["c", "a", "b"]),
    ("followers", ["a", "c", "b"], ["c", "a", "b"]),
    ("likes", ["a", "c", "b"], ["b", "c", "a"]),
    ("comments", ["b", "c", "a"], ["a", "c", "b"]),
])
def test_sort_each_key_both_directions_nulls_last(key, asc_ids, desc_ids):
    assert [r["id"] for r in feed.sort_rows(_rows_for_sort(), key, "asc")] == asc_ids
    assert [r["id"] for r in feed.sort_rows(_rows_for_sort(), key, "desc")] == desc_ids


def test_unknown_sort_falls_back_to_viral():
    assert feed.normalize_sort("bogus") == "viral"
    assert [r["id"] for r in feed.sort_rows(_rows_for_sort(), "bogus", "desc")] == ["c", "a", "b"]


def test_paginate():
    assert feed.paginate(list(range(10)), 3, 0) == [0, 1, 2]
    assert feed.paginate(list(range(10)), 3, 9) == [9]
    assert feed.paginate(list(range(10)), 3, 20) == []


# ── сборщик build_feed на фейковом db ───────────────────────────────────────

class _Resp:
    def __init__(self, data: list[dict[str, Any]], count: Optional[int] = None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, rows: list[dict[str, Any]], log: list[Any]):
        self.rows = list(rows)
        self.log = log
        self._range: Optional[tuple[int, int]] = None
        self._limit: Optional[int] = None
        self.not_ = self

    def select(self, *a: Any, **k: Any) -> "_Query":
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self

    def in_(self, col: str, vals: list[Any]) -> "_Query":
        self.rows = [r for r in self.rows if r.get(col) in vals]
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        # not_.is_(..., 'null') -> «не null»; до `not_` тут простой None-фильтр не нужен
        self.rows = [r for r in self.rows if r.get(col) is not None]
        return self

    def order(self, col: str, desc: bool = False) -> "_Query":
        self.rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        return self

    def range(self, a: int, b: int) -> "_Query":
        self._range = (a, b)
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def execute(self) -> _Resp:
        rows = self.rows
        if self._range:
            self.log.append(self._range)
            a, b = self._range
            rows = rows[a:min(b + 1, a + 1000)]  # потолок PostgREST: максимум 1000 строк
        elif self._limit is not None:
            rows = rows[:self._limit]
        else:
            rows = rows[:1000]
        return _Resp(rows)


class _FakeDB:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables
        self.range_log: list[Any] = []

    def table(self, name: str) -> _Query:
        return _Query(self.tables.get(name, []), self.range_log)


def test_light_reels_collected_page_by_page_over_1000(monkeypatch):
    reels = [reel(f"{i:05d}", "a", 100, 10) for i in range(2300)]
    db = _FakeDB({"radar_reels": reels})
    monkeypatch.setattr(repo_intel, "get_db", lambda: db)
    out = repo_intel.list_light_reels(["a"])
    assert len(out) == 2300 and len({r["id"] for r in out}) == 2300
    assert db.range_log == [(0, 999), (1000, 1999), (2000, 2999)]


def test_snapshots_collected_page_by_page(monkeypatch):
    snaps = [{"id": i, "reel_id": "r1", "views": i, "taken_at": iso(5) if i % 2 else iso(1)} for i in range(1500)]
    db = _FakeDB({"radar_reel_snapshots": snaps})
    monkeypatch.setattr(repo_intel, "get_db", lambda: db)
    out = repo_intel.list_snapshots(["r1"])
    assert len(out["r1"]) == 1500
    assert out["r1"] == sorted(out["r1"], key=lambda s: s["taken_at"])


def _fake_feed_db(monkeypatch) -> _FakeDB:
    sources = [
        {"username": "a", "followers": 1000, "is_source": True},
        {"username": "b", "followers": None, "is_source": True},
        {"username": "x", "followers": 5, "is_source": False},
    ]
    reels = mature(11, 1000, "a")
    reels += [reel("hit", "a", 5000, 2), reel("old", "a", 900, 120)]
    reels += mature(3, 800, "b")
    reels += [reel("xx", "x", 1, 3)]  # не источник — в ленте быть не должен
    full = [{**r, "url": f"https://i/{r['id']}", "caption": "c", "duration_sec": 12.0} for r in reels]
    db = _FakeDB({
        "radar_competitors": sources,
        "radar_reels": full,
        "radar_reel_snapshots": [],
        "radar_analyses": [{"reel_id": "rhit", "mode": "tv", "status": "done"}],
        "radar_refreshes": [{"status": "done", "finished_at": "2026-10-05T10:00:00+00:00"}],
    })
    monkeypatch.setattr(repo_intel, "get_db", lambda: db)
    return db


def test_build_feed_totals_and_accounts(monkeypatch):
    _fake_feed_db(monkeypatch)
    out = feed.build_feed(period=30, now=NOW)
    # a: 11 рилсов (10-20 дней) + hit (2 дня) = 12 за 30 дней; old (120) вне периода; b: 3
    assert out["total"] == 15
    assert out["accounts"] == [{"username": "a", "count": 12}, {"username": "b", "count": 3}]
    assert out["viral_threshold"] == 3.0
    assert out["last_refresh_at"] == "2026-10-05T10:00:00+00:00"
    assert out["items"][0]["id"] == "rhit"
    top = out["items"][0]
    assert top["is_viral"] is True and top["viral_ratio"] == 5.0
    assert top["analysis_status"] == "done" and top["analysis_mode"] == "tv"
    assert top["url"] == "https://i/rhit" and top["reach"] == 5.0
    assert all(i["username"] != "x" for i in out["items"])


def test_build_feed_filters(monkeypatch):
    _fake_feed_db(monkeypatch)
    only = feed.build_feed(period=30, only_viral=True, now=NOW)
    assert [i["id"] for i in only["items"]] == ["rhit"] and only["total"] == 1
    assert len(only["accounts"]) == 2 and only["accounts"][0]["count"] == 12  # accounts до only_viral
    by_user = feed.build_feed(period=30, usernames=["b"], now=NOW)
    assert by_user["total"] == 3 and {i["username"] for i in by_user["items"]} == {"b"}
    assert sum(a["count"] for a in by_user["accounts"]) == 15  # и до usernames
    all_time = feed.build_feed(period=0, now=NOW)
    assert all_time["total"] == 16
    week = feed.build_feed(period=7, now=NOW)
    assert week["total"] == 1


def test_build_feed_pagination(monkeypatch):
    _fake_feed_db(monkeypatch)
    p1 = feed.build_feed(period=30, limit=10, offset=0, now=NOW)
    p2 = feed.build_feed(period=30, limit=10, offset=10, now=NOW)
    assert len(p1["items"]) == 10 and len(p2["items"]) == 5
    assert p1["total"] == p2["total"] == 15
    assert not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]})


def test_build_feed_rising_by_history(monkeypatch):
    db = _fake_feed_db(monkeypatch)
    db.tables["radar_reel_snapshots"] = [
        {"id": 1, "reel_id": "rhit", "views": 1000, "taken_at": iso(1.5)},
        {"id": 2, "reel_id": "rhit", "views": 5000, "taken_at": iso(0.2)},
    ]
    out = feed.build_feed(period=30, now=NOW)
    top = next(i for i in out["items"] if i["id"] == "rhit")
    assert top["rising"] is True and top["rising_mode"] == "history"


def test_build_sources(monkeypatch):
    db = _fake_feed_db(monkeypatch)
    db.tables["radar_competitors"][2].update({"is_source": False, "excluded_at": "2026-10-01T00:00:00+00:00"})
    db.tables["radar_competitors"][0]["last_scraped_at"] = iso(20)
    out = feed.build_sources(now=NOW)
    a = next(s for s in out["active"] if s["username"] == "a")
    assert a["reels_count"] == 13 and a["viral_count"] == 1
    assert a["norm_n"] == 11 and a["norm_reliable"] is True and a["needs_deep"] is True
    b = next(s for s in out["active"] if s["username"] == "b")
    assert b["needs_deep"] is True and b["viral_count"] == 0 and b["median_views"] == 800
    assert out["excluded"] == [{"username": "x", "excluded_at": "2026-10-01T00:00:00+00:00", "followers": 5}]
    assert out["max_sources"] == 60
