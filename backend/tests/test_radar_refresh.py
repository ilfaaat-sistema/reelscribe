"""Тесты обновления источников Радара (ТЗ 10): оценка, запуск, шаг конвейера, снимки.

БД и Apify не трогаются: repo/apify_runs подменяются фейковым хранилищем в памяти
(класс FakeDB), роутер тестируется отдельным FastAPI() без app.main."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.radar.settings as rs
from app.api import radar_refresh
from app.radar import apify_runs, refresh_service, repo, scrape_service

_app = FastAPI()
_app.include_router(radar_refresh.router, prefix="/api")
client = TestClient(_app)


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


class FakeDB:
    """Фейковое хранилище с теми же функциями, что использует refresh_service."""

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources
        self.refreshes: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.scraped: list[str] = []
        self.refreshes_last_hour = 0
        self.started: list[int] = []        # run_id, для которых вызван Apify
        self.start_fail: set[int] = set()   # run_id, у которых старт Apify падает
        self.outcome: dict[int, str] = {}   # run_id -> "done"|"error" при опросе
        self.results: dict[int, int] = {}
        self.costs: dict[int, float] = {}

    # repo
    def list_active_sources(self) -> list[dict[str, Any]]:
        return list(self.sources)

    def close_stale_refreshes(self, older_than_min: int) -> int:
        return 0

    def get_running_refresh(self) -> Optional[dict[str, Any]]:
        return next((r for r in self.refreshes if r["status"] == "running"), None)

    def get_latest_refresh(self) -> Optional[dict[str, Any]]:
        return self.refreshes[-1] if self.refreshes else None

    def count_refreshes_last_hour(self) -> int:
        return self.refreshes_last_hour

    def create_refresh(self, total_batches: int, estimate_usd: Optional[float]) -> dict[str, Any]:
        row = {"id": len(self.refreshes) + 1, "status": "running", "total_batches": total_batches,
               "estimate_usd": estimate_usd, "cost_usd": None, "reels_saved": 0, "errors": [],
               "created_at": _iso(), "finished_at": None}
        self.refreshes.append(row)
        return row

    def create_scrape_run(self, kind, usernames, period_months, refresh_id=None, batch_idx=None,
                          params=None) -> dict[str, Any]:
        row = {"id": 100 + len(self.runs), "kind": kind, "usernames": usernames,
               "period_months": period_months, "status": "pending", "refresh_id": refresh_id,
               "batch_idx": batch_idx, "params": params, "start_claimed_at": None,
               "results_count": None, "cost_estimate_usd": None, "error": None}
        self.runs.append(row)
        return row

    def list_refresh_runs(self, refresh_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in sorted(self.runs, key=lambda x: x["batch_idx"])
                if r["refresh_id"] == refresh_id]

    def get_scrape_run(self, run_id: int) -> Optional[dict[str, Any]]:
        return next((dict(r) for r in self.runs if r["id"] == run_id), None)

    def claim_run_start(self, run_id: int) -> bool:
        run = next(r for r in self.runs if r["id"] == run_id)
        if run["status"] == "pending" and run["start_claimed_at"] is None:
            run["start_claimed_at"] = _iso()
            return True
        return False

    def mark_run_error(self, run_id: int, error: str) -> None:
        run = next(r for r in self.runs if r["id"] == run_id)
        if run["status"] in ("pending", "running"):
            run["status"] = "error"
            run["error"] = error

    def finalize_refresh(self, refresh_id, status, reels_saved, cost_usd, errors) -> bool:
        row = self.refreshes[refresh_id - 1]
        if row["status"] != "running":
            return False
        row.update(status=status, reels_saved=reels_saved, cost_usd=cost_usd, errors=errors,
                   finished_at=_iso())
        return True

    def set_last_scraped(self, usernames: list[str]) -> None:
        self.scraped.extend(usernames)

    # scrape_service
    async def start_refresh_batch(self, run: dict[str, Any]) -> None:
        await asyncio.sleep(0)  # точка переключения — для теста гонки
        if run["id"] in self.start_fail:
            raise scrape_service.RadarApifyError("Apify недоступен")
        self.started.append(run["id"])
        next(r for r in self.runs if r["id"] == run["id"])["status"] = "running"

    async def poll_scrape_run(self, run_id: int) -> Optional[dict[str, Any]]:
        run = next(r for r in self.runs if r["id"] == run_id)
        if run["status"] == "running" and run_id in self.outcome:
            run["status"] = self.outcome[run_id]
            if run["status"] == "done":
                run["results_count"] = self.results.get(run_id, 0)
                run["cost_estimate_usd"] = self.costs.get(run_id, 0.0)
            else:
                run["error"] = "Прогон Apify завершился статусом FAILED"
        return dict(run)


def _install(monkeypatch, db: FakeDB) -> FakeDB:
    for name in ("list_active_sources", "close_stale_refreshes", "get_running_refresh",
                 "get_latest_refresh", "count_refreshes_last_hour", "create_refresh",
                 "create_scrape_run", "list_refresh_runs", "get_scrape_run", "claim_run_start",
                 "mark_run_error", "finalize_refresh", "set_last_scraped"):
        monkeypatch.setattr(repo, name, getattr(db, name))
    monkeypatch.setattr(scrape_service, "start_refresh_batch", db.start_refresh_batch)
    monkeypatch.setattr(scrape_service, "poll_scrape_run", db.poll_scrape_run)
    return db


def _src(name: str, scraped: Optional[timedelta] = None, followers: Optional[timedelta] = None):
    return {"username": name, "followers": 1000,
            "last_scraped_at": _iso(scraped) if scraped is not None else None,
            "followers_updated_at": _iso(followers) if followers is not None else None}


def _step() -> dict[str, Any]:
    return asyncio.run(refresh_service.current_step())


# ── estimate ─────────────────────────────────────────────────────────────────

def test_estimate_deep_incremental_skipped(monkeypatch):
    sources = (
        [_src(f"d{i}") for i in range(3)]                                   # новые → глубокие
        + [_src("old", scraped=timedelta(days=30), followers=timedelta(days=10))]  # давно → глубокий
        + [_src(f"l{i}", scraped=timedelta(days=2), followers=timedelta(hours=1)) for i in range(2)]
        + [_src("fresh", scraped=timedelta(hours=1))]                       # свежий → пропуск
    )
    _install(monkeypatch, FakeDB(sources))
    resp = client.get("/api/radar/refresh/estimate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_total"] == 7
    assert data["deep"] == 4 and data["incremental"] == 2
    assert data["to_refresh"] == 6 and data["skipped_fresh"] == 1
    # пачки: глубоких 4/5 → 1, лёгких 2/10 → 1, подписчики: 4 глубоких без свежих → 1
    assert data["followers_profiles"] == 4
    assert data["batches"] == 3
    assert data["reels_est"] == 4 * rs.DEEP_LIMIT + 2 * rs.INCR_LIMIT
    assert data["reels_usd"] == round(data["reels_est"] * rs.APIFY_COST_PER_REEL, 2)
    total = data["reels_est"] * rs.APIFY_COST_PER_REEL + 4 * rs.APIFY_COST_PER_PROFILE
    assert data["total_usd"] == round(total, 2)
    assert data["total_rub"] == float(round(total * 80))
    assert data["rate"] == 80


def test_estimate_example_from_spec(monkeypatch):
    sources = [_src(f"d{i}") for i in range(3)] + [
        _src(f"l{i}", scraped=timedelta(days=2)) for i in range(17)
    ]
    _install(monkeypatch, FakeDB(sources))
    data = client.get("/api/radar/refresh/estimate").json()
    assert data["reels_est"] == 435 and data["reels_usd"] == 1.13
    assert data["followers_usd"] == 0.05 and data["total_usd"] == 1.18
    assert data["total_rub"] == 94.0
    assert data["batches"] == 4  # 1 глубокая + 2 лёгких + 1 пачка подписчиков (в ТЗ «6» — иллюстрация)


# ── start ────────────────────────────────────────────────────────────────────

def test_start_creates_batches_in_order(monkeypatch):
    sources = (
        [_src(f"d{i}") for i in range(6)]                                    # 6 глубоких → 2 пачки
        + [_src(f"l{i}", scraped=timedelta(days=2), followers=timedelta(days=5)) for i in range(11)]
    )
    db = _install(monkeypatch, FakeDB(sources))
    resp = client.post("/api/radar/refresh", json={"force": False})
    assert resp.status_code == 200
    assert resp.json() == {"refresh_id": 1}

    kinds = [(r["kind"], len(r["usernames"]), r["batch_idx"]) for r in db.runs]
    assert kinds == [("reels", 5, 1), ("reels", 1, 2), ("reels", 10, 3), ("reels", 1, 4),
                     ("followers", 17, 5)]
    deep, incr = db.runs[0]["params"], db.runs[2]["params"]
    assert deep["deep"] is True and deep["results_limit"] == rs.DEEP_LIMIT
    assert incr["deep"] is False and incr["results_limit"] == rs.INCR_LIMIT
    today = datetime.now(timezone.utc).date()
    assert deep["newer_than"] == (today - timedelta(days=rs.DEEP_DAYS)).isoformat()
    assert incr["newer_than"] == (today - timedelta(days=rs.INCR_DAYS)).isoformat()
    assert db.runs[4]["params"] == {}
    assert all(r["period_months"] is None and r["refresh_id"] == 1 for r in db.runs)
    assert db.refreshes[0]["total_batches"] == 5


def test_start_no_sources_400(monkeypatch):
    _install(monkeypatch, FakeDB([]))
    resp = client.post("/api/radar/refresh", json={})
    assert resp.status_code == 400
    assert "источник" in resp.json()["detail"].lower()


def test_start_all_fresh_400_and_force(monkeypatch):
    db = _install(monkeypatch, FakeDB([_src("a", scraped=timedelta(hours=1), followers=timedelta(hours=1))]))
    assert client.post("/api/radar/refresh", json={"force": False}).status_code == 400
    assert client.post("/api/radar/refresh", json={"force": True}).status_code == 200
    assert db.runs[0]["params"]["deep"] is False


def test_start_conflict_409(monkeypatch):
    db = _install(monkeypatch, FakeDB([_src("a")]))
    db.create_refresh(1, 0.1)
    resp = client.post("/api/radar/refresh", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]


def test_start_hourly_limit_429(monkeypatch):
    db = _install(monkeypatch, FakeDB([_src("a")]))
    db.refreshes_last_hour = rs.REFRESHES_PER_HOUR
    resp = client.post("/api/radar/refresh", json={})
    assert resp.status_code == 429


def test_start_unique_violation_maps_to_409(monkeypatch):
    db = _install(monkeypatch, FakeDB([_src("a")]))

    def boom(total, est):
        raise RuntimeError('duplicate key value violates unique constraint "uq_radar_refresh_one_running"')

    monkeypatch.setattr(repo, "create_refresh", boom)
    assert client.post("/api/radar/refresh", json={}).status_code == 409
    assert db.runs == []


# ── current ──────────────────────────────────────────────────────────────────

def test_current_idle(monkeypatch):
    _install(monkeypatch, FakeDB([]))
    assert client.get("/api/radar/refresh/current").json() == {"status": "idle"}


def _started(monkeypatch, sources) -> FakeDB:
    db = _install(monkeypatch, FakeDB(sources))
    refresh_service.start()
    return db


def test_current_pending_claims_and_starts(monkeypatch):
    db = _started(monkeypatch, [_src("a", followers=timedelta(hours=1))])
    data = client.get("/api/radar/refresh/current").json()
    assert db.started == [db.runs[0]["id"]]
    assert db.runs[0]["start_claimed_at"] is not None
    assert data["status"] == "running" and data["stage"] == "reels"
    assert data["stage_label"] == "рилсы 1/1 пачки"
    assert data["batch_total"] == 1 and data["batch_done"] == 0


def test_current_race_starts_apify_once(monkeypatch):
    db = _started(monkeypatch, [_src("a", followers=timedelta(hours=1))])

    async def two_calls():
        return await asyncio.gather(refresh_service.current_step(), refresh_service.current_step())

    asyncio.run(two_calls())
    assert len(db.started) == 1


def test_current_running_then_next_batch(monkeypatch):
    # 6 глубоких → две реел-пачки + пачка подписчиков
    db = _started(monkeypatch, [_src(f"d{i}") for i in range(6)])
    first, second, fol = (r["id"] for r in db.runs)
    _step()
    assert db.started == [first]
    db.outcome[first] = "done"
    db.results[first] = 120
    db.costs[first] = 0.31
    data = _step()  # приём первой пачки и старт второй — в одном вызове
    assert db.started == [first, second]
    assert data["status"] == "running" and data["stage_label"] == "рилсы 2/2 пачки"
    assert data["batch_done"] == 1 and data["reels_saved"] == 120
    assert data["cost_usd"] == 0.31 and data["cost_rub"] == 25.0
    assert fol not in db.started


def test_current_partial_error_does_not_stop_and_finalizes(monkeypatch):
    db = _started(monkeypatch, [_src(f"d{i}") for i in range(6)])
    first, second, fol = (r["id"] for r in db.runs)
    _step()
    db.outcome[first] = "error"
    _step()                        # первая упала, стартовала вторая
    assert db.started == [first, second]
    db.outcome[second] = "done"
    db.results[second] = 7
    db.costs[second] = 0.1
    _step()                        # вторая принята, стартовали подписчики
    assert db.started == [first, second, fol]
    db.outcome[fol] = "done"
    db.costs[fol] = 0.02
    data = _step()
    assert data["status"] == "done" and data["stage"] is None
    assert data["reels_saved"] == 7
    assert data["errors"] == [{"batch": 1, "usernames": ["d0", "d1", "d2", "d3", "d4"],
                               "error": "Прогон Apify завершился статусом FAILED"}]
    # last_scraped_at — только никам успешной реел-пачки (ставится по пачке и повторно,
    # идемпотентно, в финале)
    assert set(db.scraped) == {"d5"}
    # повторный вызов после завершения — без продвижения, то же состояние
    again = _step()
    assert again["status"] == "done" and again["reels_saved"] == 7
    assert db.started == [first, second, fol]


def test_current_all_failed_is_error(monkeypatch):
    db = _started(monkeypatch, [_src("a", followers=timedelta(hours=1))])
    _step()
    db.outcome[db.runs[0]["id"]] = "error"
    data = _step()
    assert data["status"] == "error"
    assert db.scraped == []
    assert len(data["errors"]) == 1


def test_current_start_failure_marks_batch_and_goes_on(monkeypatch):
    db = _started(monkeypatch, [_src(f"d{i}") for i in range(6)])
    first, second, _fol = (r["id"] for r in db.runs)
    db.start_fail.add(first)
    data = _step()
    assert db.runs[0]["status"] == "error"
    assert db.started == [second]  # без повторного запуска первой, следующая стартовала
    assert data["errors"][0]["batch"] == 1


def test_current_stale_claim_marks_error(monkeypatch):
    db = _started(monkeypatch, [_src("a", followers=timedelta(hours=1))])
    run = db.runs[0]
    run["start_claimed_at"] = _iso(timedelta(seconds=rs.START_CLAIM_STALE_SEC + 10))
    data = _step()
    assert run["status"] == "error" and db.started == []
    assert data["status"] == "error"


def test_current_fresh_foreign_claim_waits(monkeypatch):
    db = _started(monkeypatch, [_src("a", followers=timedelta(hours=1))])
    db.runs[0]["start_claimed_at"] = _iso()
    data = _step()
    assert db.started == [] and db.runs[0]["status"] == "pending"
    assert data["status"] == "running"


# ── снимки ───────────────────────────────────────────────────────────────────

def _snapshot_env(monkeypatch, snap):
    store = {1: {"id": 1, "kind": "reels", "status": "running", "period_months": None,
                 "refresh_id": 5, "apify_run_id": "r", "apify_dataset_id": "d",
                 "apify_token_ref": "t"}}
    done: dict[str, Any] = {}

    async def fake_get_run(run_id, token_ref):
        return {"status": "SUCCEEDED", "dataset_id": "d", "cost_usd": 0.42}

    async def fake_items(dataset_id, token_ref):
        return [{"shortCode": "A1", "ownerUsername": "acc", "videoViewCount": 10}]

    monkeypatch.setattr(repo, "get_scrape_run", lambda rid: dict(store[rid]))
    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)
    monkeypatch.setattr(apify_runs, "get_items", fake_items)
    monkeypatch.setattr(repo, "try_claim_scrape_finish", lambda rid: True)
    monkeypatch.setattr(repo, "ensure_competitors", lambda names: None)
    monkeypatch.setattr(repo, "bulk_upsert_reels", lambda reels, scrape_run_id: len(reels))
    monkeypatch.setattr(repo, "insert_reel_snapshots", snap)
    monkeypatch.setattr(repo, "finish_scrape_run_done",
                        lambda rid, n, cost: done.update(n=n, cost=cost))
    monkeypatch.setattr(repo, "release_scrape_finish_claim",
                        lambda rid: (_ for _ in ()).throw(AssertionError("приём не должен откатываться")))
    return done


def test_poll_writes_snapshots(monkeypatch):
    calls: list[tuple[int, int]] = []
    done = _snapshot_env(
        monkeypatch, lambda reels, scrape_run_id: calls.append((len(reels), scrape_run_id)) or 1,
    )
    asyncio.run(scrape_service.poll_scrape_run(1))
    assert calls == [(1, 1)]
    assert done == {"n": 1, "cost": 0.42}  # фактическая цена из Apify вместо оценки


def test_snapshot_failure_does_not_break_ingest(monkeypatch):
    def boom(reels, scrape_run_id):
        raise RuntimeError("таблицы снимков нет")

    done = _snapshot_env(monkeypatch, boom)
    asyncio.run(scrape_service.poll_scrape_run(1))
    assert done["n"] == 1


@pytest.mark.parametrize("period", [None])
def test_no_cutoff_for_refresh_batches(period):
    old = {"id": "x", "posted_at": "2020-01-01T00:00:00+00:00"}
    assert scrape_service._filter_by_cutoff([old], period) == [old]
