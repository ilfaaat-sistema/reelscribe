"""Тесты роутера /api/radar/* (агент A).

БД/Apify/pipeline мокаются monkeypatch'ем на уровне модулей (app.radar.repo,
app.radar.apify_runs, app.radar.scrape_service, app.radar.pipeline) — ни один тест не ходит
в реальный Supabase/Apify/Gemini. app.radar.pipeline (агент B) уже существует к моменту
написания этого файла — мокаются его функции напрямую, без подмены sys.modules.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.radar.settings as radar_settings
from app.main import app
from app.radar import apify_runs, pipeline, repo, scrape_service

client = TestClient(app)


# ── /scrape: лимиты и старт ──────────────────────────────────────────────────

def test_scrape_empty_usernames():
    resp = client.post("/api/radar/scrape", json={"usernames": [], "period_months": 3})
    assert resp.status_code == 400


def test_scrape_too_many_usernames():
    names = [f"acc{i}" for i in range(radar_settings.MAX_USERNAMES + 1)]
    resp = client.post("/api/radar/scrape", json={"usernames": names, "period_months": 3})
    assert resp.status_code == 400


def test_scrape_period_out_of_range():
    resp = client.post("/api/radar/scrape", json={"usernames": ["acc1"], "period_months": 13})
    assert resp.status_code == 400


def test_scrape_hourly_limit(monkeypatch):
    monkeypatch.setattr(repo, "count_runs_last_hour", lambda: radar_settings.SCRAPES_PER_HOUR)
    resp = client.post("/api/radar/scrape", json={"usernames": ["acc1"], "period_months": 3})
    assert resp.status_code == 429


def test_scrape_apify_error(monkeypatch):
    monkeypatch.setattr(repo, "count_runs_last_hour", lambda: 0)
    monkeypatch.setattr(repo, "create_scrape_run", lambda **kw: {"id": 1})
    errored = []
    monkeypatch.setattr(repo, "finish_scrape_run_error", lambda run_id, err: errored.append((run_id, err)))

    async def fake_start_run(actor, run_input):
        raise apify_runs.ApifyRunError("Apify недоступен")

    monkeypatch.setattr(apify_runs, "start_run", fake_start_run)

    resp = client.post("/api/radar/scrape", json={"usernames": ["acc1"], "period_months": 3})
    assert resp.status_code == 502
    assert errored == [(1, "Apify недоступен")]


def test_scrape_success(monkeypatch):
    monkeypatch.setattr(repo, "count_runs_last_hour", lambda: 0)
    monkeypatch.setattr(repo, "create_scrape_run", lambda **kw: {"id": 7})
    started = {}

    async def fake_start_run(actor, run_input):
        assert actor == radar_settings.REEL_ACTOR
        assert run_input["username"] == ["acc1"]
        return {"run_id": "runX", "dataset_id": "dsX", "token_ref": "tokX"}

    def fake_set_started(run_id, apify_run_id, apify_dataset_id, apify_token_ref):
        started.update(run_id=run_id, apify_run_id=apify_run_id,
                        apify_dataset_id=apify_dataset_id, apify_token_ref=apify_token_ref)

    monkeypatch.setattr(apify_runs, "start_run", fake_start_run)
    monkeypatch.setattr(repo, "set_run_started", fake_set_started)

    resp = client.post("/api/radar/scrape", json={"usernames": ["@acc1"], "period_months": 3})
    assert resp.status_code == 200
    assert resp.json() == {"run_id": 7}
    assert started == {
        "run_id": 7, "apify_run_id": "runX", "apify_dataset_id": "dsX", "apify_token_ref": "tokX",
    }


def test_scrape_cost_estimate():
    resp = client.get("/api/radar/scrape/cost-estimate", params={"usernames": "a,b", "period_months": 2})
    assert resp.status_code == 200
    n_reels = 2 * radar_settings.REELS_PER_MONTH_ESTIMATE * 2
    assert resp.json() == {
        "estimate_usd": round(n_reels * radar_settings.APIFY_COST_PER_REEL, 2),
        "n_reels": n_reels,
    }


def test_scrape_status_not_found(monkeypatch):
    monkeypatch.setattr(repo, "get_scrape_run", lambda run_id: None)
    resp = client.get("/api/radar/scrape/999")
    assert resp.status_code == 404


# ── /scrape/{id}: идемпотентность повторного опроса (на уровне сервиса) ─────

def test_poll_scrape_run_idempotent(monkeypatch):
    calls = {"get_run": 0, "get_items": 0}
    upserts: list[int] = []
    store = {
        1: {
            "id": 1, "kind": "reels", "status": "running", "period_months": None,
            "apify_run_id": "run1", "apify_dataset_id": "ds1", "apify_token_ref": "tok1",
        },
    }

    def fake_get_scrape_run(run_id):
        return dict(store[run_id]) if run_id in store else None

    async def fake_get_run(run_id, token_ref):
        calls["get_run"] += 1
        return {"status": "SUCCEEDED", "dataset_id": "ds1"}

    async def fake_get_items(dataset_id, token_ref):
        calls["get_items"] += 1
        return [{"shortCode": "ABC123", "ownerUsername": "acc1", "url": "u", "videoViewCount": 100}]

    def fake_ensure_competitors(usernames):
        pass

    def fake_bulk_upsert(reels, scrape_run_id):
        upserts.append(len(reels))
        return len(reels)

    def fake_finish_done(run_id, results_count, cost_estimate_usd):
        store[run_id]["status"] = "done"
        store[run_id]["results_count"] = results_count
        store[run_id]["cost_estimate_usd"] = cost_estimate_usd
        return store[run_id]

    def fail_release(run_id):
        raise AssertionError("release_scrape_finish_claim не должен вызываться — приём не падал")

    monkeypatch.setattr(repo, "get_scrape_run", fake_get_scrape_run)
    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)
    monkeypatch.setattr(apify_runs, "get_items", fake_get_items)
    monkeypatch.setattr(repo, "try_claim_scrape_finish", lambda run_id: True)
    monkeypatch.setattr(repo, "release_scrape_finish_claim", fail_release)
    monkeypatch.setattr(repo, "ensure_competitors", fake_ensure_competitors)
    monkeypatch.setattr(repo, "bulk_upsert_reels", fake_bulk_upsert)
    monkeypatch.setattr(repo, "finish_scrape_run_done", fake_finish_done)

    result1 = asyncio.run(scrape_service.poll_scrape_run(1))
    assert result1["status"] == "done"
    assert result1["results_count"] == 1
    assert calls == {"get_run": 1, "get_items": 1}
    assert upserts == [1]

    # повторный опрос уже done-прогона — Apify не трогаем, строки не дублируем/не пересчитываем
    result2 = asyncio.run(scrape_service.poll_scrape_run(1))
    assert result2["status"] == "done"
    assert calls == {"get_run": 1, "get_items": 1}
    assert upserts == [1]


def test_poll_scrape_run_second_parallel_poll_does_not_double_accept(monkeypatch):
    """Гонка двух параллельных GET /scrape/{id} (фронт поллит раз в 2 с): оба видят Apify
    SUCCEEDED, но try_claim_scrape_finish отдаёт True только одному. Второй не должен ни
    трогать get_items/bulk_upsert, ни падать — просто отдаёт текущее состояние."""
    run = {
        "id": 1, "kind": "reels", "status": "running", "period_months": None,
        "apify_run_id": "run1", "apify_dataset_id": "ds1", "apify_token_ref": "tok1",
    }
    monkeypatch.setattr(repo, "get_scrape_run", lambda run_id: dict(run))

    async def fake_get_run(run_id, token_ref):
        return {"status": "SUCCEEDED", "dataset_id": "ds1"}

    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)
    monkeypatch.setattr(repo, "try_claim_scrape_finish", lambda run_id: False)

    def fail(*a, **kw):
        raise AssertionError("не должно вызываться — приём уже захвачен другим опросом")

    monkeypatch.setattr(apify_runs, "get_items", fail)
    monkeypatch.setattr(repo, "bulk_upsert_reels", fail)
    monkeypatch.setattr(repo, "finish_scrape_run_done", fail)
    monkeypatch.setattr(repo, "finish_scrape_run_error", fail)

    result = asyncio.run(scrape_service.poll_scrape_run(1))
    assert result["status"] == "running"


def test_poll_scrape_run_apify_network_error_keeps_running_no_error(monkeypatch):
    """Сетевой сбой/5xx/токен в cooldown при опросе статуса — НЕ терминальная ошибка Apify:
    сам прогон продолжается и может завершиться успехом. error не пишется, статус остаётся
    running, следующий опрос (через 2 с) попробует снова."""
    run = {
        "id": 1, "kind": "reels", "status": "running", "period_months": None,
        "apify_run_id": "run1", "apify_dataset_id": "ds1", "apify_token_ref": "tok1",
    }
    monkeypatch.setattr(repo, "get_scrape_run", lambda run_id: dict(run))

    async def fake_get_run(run_id, token_ref):
        raise apify_runs.ApifyRunError("Apify: опрос прогона run1 HTTP 503 upstream timeout")

    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)

    def fail(*a, **kw):
        raise AssertionError("error писаться не должен — сбой не терминальный")

    monkeypatch.setattr(repo, "finish_scrape_run_error", fail)

    result = asyncio.run(scrape_service.poll_scrape_run(1))
    assert result["status"] == "running"
    assert result.get("error") is None


def test_poll_scrape_run_dedupes_duplicate_shortcode_keeps_higher_views(monkeypatch):
    """Коллаб-рилс двух конкурентов из списка ников попадает в датасет дважды с одинаковым
    shortCode. Без дедупа bulk_upsert_reels получил бы два UPDATE на один PK в одной пачке —
    Postgres «ON CONFLICT DO UPDATE command cannot affect row a second time»."""
    run = {
        "id": 1, "kind": "reels", "status": "running", "period_months": None,
        "apify_run_id": "run1", "apify_dataset_id": "ds1", "apify_token_ref": "tok1",
    }
    monkeypatch.setattr(repo, "get_scrape_run", lambda run_id: dict(run))

    async def fake_get_run(run_id, token_ref):
        return {"status": "SUCCEEDED", "dataset_id": "ds1"}

    async def fake_get_items(dataset_id, token_ref):
        return [
            {"shortCode": "DUP1", "ownerUsername": "acc1", "url": "u", "videoViewCount": 100},
            {"shortCode": "DUP1", "ownerUsername": "acc2", "url": "u", "videoViewCount": 250},
        ]

    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)
    monkeypatch.setattr(apify_runs, "get_items", fake_get_items)
    monkeypatch.setattr(repo, "try_claim_scrape_finish", lambda run_id: True)
    monkeypatch.setattr(repo, "ensure_competitors", lambda usernames: None)

    captured: list[dict] = []
    monkeypatch.setattr(
        repo, "bulk_upsert_reels",
        lambda reels, scrape_run_id: (captured.extend(reels), len(reels))[1],
    )
    monkeypatch.setattr(repo, "finish_scrape_run_done", lambda *a: None)

    asyncio.run(scrape_service.poll_scrape_run(1))

    assert len(captured) == 1
    assert captured[0]["id"] == "DUP1"
    assert captured[0]["views"] == 250  # оставили запись с бо́льшими просмотрами


def test_poll_scrape_run_filters_reels_older_than_cutoff(monkeypatch):
    """Рилсы старше cutoff (now − period_months) не сохраняются и не считаются в
    results_count/стоимости — поведение сверено со старым Радаром (apify_scraper.py::scrape)."""
    run = {
        "id": 1, "kind": "reels", "status": "running", "period_months": 1,
        "apify_run_id": "run1", "apify_dataset_id": "ds1", "apify_token_ref": "tok1",
    }
    monkeypatch.setattr(repo, "get_scrape_run", lambda run_id: dict(run))

    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()

    async def fake_get_run(run_id, token_ref):
        return {"status": "SUCCEEDED", "dataset_id": "ds1"}

    async def fake_get_items(dataset_id, token_ref):
        return [
            {"shortCode": "OLD1", "ownerUsername": "acc1", "url": "u", "videoViewCount": 1, "timestamp": old_ts},
            {"shortCode": "NEW1", "ownerUsername": "acc1", "url": "u", "videoViewCount": 1, "timestamp": fresh_ts},
        ]

    monkeypatch.setattr(apify_runs, "get_run", fake_get_run)
    monkeypatch.setattr(apify_runs, "get_items", fake_get_items)
    monkeypatch.setattr(repo, "try_claim_scrape_finish", lambda run_id: True)
    monkeypatch.setattr(repo, "ensure_competitors", lambda usernames: None)

    captured: list[dict] = []
    monkeypatch.setattr(
        repo, "bulk_upsert_reels",
        lambda reels, scrape_run_id: (captured.extend(reels), len(reels))[1],
    )
    finished: dict = {}
    monkeypatch.setattr(
        repo, "finish_scrape_run_done",
        lambda run_id, results_count, cost_estimate_usd: finished.update(
            results_count=results_count, cost=cost_estimate_usd,
        ),
    )

    asyncio.run(scrape_service.poll_scrape_run(1))

    assert [r["id"] for r in captured] == ["NEW1"]
    assert finished["results_count"] == 1


# ── /competitors ──────────────────────────────────────────────────────────

def test_list_competitors(monkeypatch):
    monkeypatch.setattr(
        repo, "list_competitors",
        lambda: [{"username": "acc1", "followers": 1000, "followers_updated_at": None}],
    )
    resp = client.get("/api/radar/competitors")
    assert resp.status_code == 200
    assert resp.json() == [{"username": "acc1", "followers": 1000, "followers_updated_at": None}]


def test_refresh_followers_no_accounts(monkeypatch):
    monkeypatch.setattr(repo, "list_distinct_reel_usernames", list)
    resp = client.post("/api/radar/competitors/refresh-followers", json={})
    assert resp.status_code == 400


def test_refresh_followers_hourly_limit(monkeypatch):
    monkeypatch.setattr(repo, "list_distinct_reel_usernames", lambda: ["acc1"])
    monkeypatch.setattr(repo, "count_runs_last_hour", lambda: radar_settings.SCRAPES_PER_HOUR)
    resp = client.post("/api/radar/competitors/refresh-followers", json={})
    assert resp.status_code == 429


def test_refresh_followers_success(monkeypatch):
    monkeypatch.setattr(repo, "count_runs_last_hour", lambda: 0)
    monkeypatch.setattr(repo, "create_scrape_run", lambda **kw: {"id": 3})
    monkeypatch.setattr(repo, "set_run_started", lambda *a: None)

    async def fake_start_run(actor, run_input):
        assert actor == radar_settings.PROFILE_ACTOR
        assert run_input == {"usernames": ["acc1"]}
        return {"run_id": "r", "dataset_id": "d", "token_ref": "t"}

    monkeypatch.setattr(apify_runs, "start_run", fake_start_run)

    resp = client.post("/api/radar/competitors/refresh-followers", json={"usernames": ["acc1"]})
    assert resp.status_code == 200
    assert resp.json() == {"run_id": 3}


# ── /summary, /reels ─────────────────────────────────────────────────────

def test_summary_empty(monkeypatch):
    monkeypatch.setattr(repo, "list_reels_for_summary", lambda usernames: [])
    monkeypatch.setattr(repo, "list_competitors", list)
    resp = client.get("/api/radar/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_reels"] == 0
    assert data["median_views"] is None
    assert data["best_account"] is None
    assert len(data["buckets"]) == 5
    assert data["accounts"] == []


def test_get_reels_merges_analysis_status(monkeypatch):
    monkeypatch.setattr(repo, "list_reels", lambda **kw: [{"id": "R1", "username": "acc1", "views": 10}])
    monkeypatch.setattr(repo, "list_analyses_status", lambda ids: {"R1": {"mode": "tv", "status": "done"}})
    resp = client.get("/api/radar/reels", params={"usernames": "acc1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["analysis_status"] == "done"
    assert data[0]["analysis_mode"] == "tv"


# ── /analyze ─────────────────────────────────────────────────────────────

def test_analyze_reel_not_found(monkeypatch):
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {})
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: set())
    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "MISSING", "mode": "t"}]})
    assert resp.status_code == 404


def test_analyze_daily_limit(monkeypatch):
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {i: {"id": i} for i in ids})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {})
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: set())
    monkeypatch.setattr(repo, "count_jobs_created_today", lambda: radar_settings.ANALYSES_PER_DAY)
    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "R1", "mode": "tv"}]})
    assert resp.status_code == 429


def test_analyze_skip_already_done_without_force(monkeypatch):
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {"R1": {"id": "R1"}})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {
        "R1": {
            "status": "done",
            "transcript_segments": [{"start": 0, "text": "x"}],
            "visual_timeline": [{"t": "0:00", "text": "y"}],
        },
    })
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: set())
    monkeypatch.setattr(repo, "count_jobs_created_today", lambda: 0)
    calls: list[tuple] = []
    monkeypatch.setattr(repo, "upsert_analysis_queued", lambda *a: calls.append(a))
    monkeypatch.setattr(repo, "create_job", lambda *a: calls.append(a))

    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "R1", "mode": "tv"}], "force": False})
    assert resp.status_code == 200
    assert resp.json() == {"queued": [], "skipped": ["R1"]}
    assert calls == []


def test_analyze_mode_v_not_skipped_when_only_transcript_done(monkeypatch):
    """Готов только транскрипт (has_t), запрошен режим 'v' — это НЕ закрытая задача для 'v',
    рилс ставится в очередь заново, а не в skipped."""
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {"R1": {"id": "R1"}})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {
        "R1": {
            "status": "done",
            "transcript_segments": [{"start": 0, "text": "x"}],
            "visual_timeline": None,
        },
    })
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: set())
    monkeypatch.setattr(repo, "count_jobs_created_today", lambda: 0)
    calls: list[tuple] = []
    monkeypatch.setattr(repo, "upsert_analysis_queued", lambda reel_id, mode: calls.append((reel_id, mode)))
    monkeypatch.setattr(repo, "create_job", lambda reel_id, mode: calls.append((reel_id, mode)))

    async def fake_refresh(reel_ids):
        pass

    monkeypatch.setattr(pipeline, "refresh_expired_links", fake_refresh)

    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "R1", "mode": "v"}], "force": False})
    assert resp.status_code == 200
    assert resp.json() == {"queued": ["R1"], "skipped": []}
    assert ("R1", "v") in calls


def test_analyze_skip_reel_with_active_job_even_with_force(monkeypatch):
    """У рилса уже есть незавершённое задание (queued/in_progress) — повторная постановка
    идёт в skipped, force это правило не отменяет: иначе задание разбиралось бы дважды."""
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {"R1": {"id": "R1"}})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {})
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: {"R1"})
    monkeypatch.setattr(repo, "count_jobs_created_today", lambda: 0)
    calls: list[tuple] = []
    monkeypatch.setattr(repo, "upsert_analysis_queued", lambda *a: calls.append(a))
    monkeypatch.setattr(repo, "create_job", lambda *a: calls.append(a))

    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "R1", "mode": "tv"}], "force": True})
    assert resp.status_code == 200
    assert resp.json() == {"queued": [], "skipped": ["R1"]}
    assert calls == []


def test_analyze_queues_new_job_and_refreshes_links(monkeypatch):
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {"R2": {"id": "R2"}})
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {})
    monkeypatch.setattr(repo, "get_active_job_reel_ids", lambda ids: set())
    monkeypatch.setattr(repo, "count_jobs_created_today", lambda: 0)

    calls: list[tuple] = []
    monkeypatch.setattr(repo, "upsert_analysis_queued", lambda reel_id, mode: calls.append(("analysis", reel_id, mode)))
    monkeypatch.setattr(repo, "create_job", lambda reel_id, mode: calls.append(("job", reel_id, mode)))

    refreshed = []

    async def fake_refresh(reel_ids):
        refreshed.append(list(reel_ids))

    monkeypatch.setattr(pipeline, "refresh_expired_links", fake_refresh)

    resp = client.post("/api/radar/analyze", json={"items": [{"reel_id": "R2", "mode": "t"}]})
    assert resp.status_code == 200
    assert resp.json() == {"queued": ["R2"], "skipped": []}
    assert ("analysis", "R2", "t") in calls
    assert ("job", "R2", "t") in calls
    assert refreshed == [["R2"]]


# ── /jobs/{id}/run, /jobs/tick ───────────────────────────────────────────

def test_jobs_run_already_claimed_returns_current_status(monkeypatch):
    """Два одновременных /run одного рилса не разбирают его дважды: claim_job вернул None
    (уже захвачено), роутер не запускает process_radar_job повторно."""
    monkeypatch.setattr(pipeline, "claim_job", lambda reel_id: None)
    monkeypatch.setattr(repo, "get_analysis", lambda reel_id: {"status": "in_progress"})
    resp = client.post("/api/radar/jobs/R1/run")
    assert resp.status_code == 200
    assert resp.json() == {"reel_id": "R1", "status": "in_progress"}


def test_jobs_run_processes_claimed_job(monkeypatch):
    job = {"id": "job1", "reel_id": "R1", "mode": "t"}
    monkeypatch.setattr(pipeline, "claim_job", lambda reel_id: job)

    async def fake_process(j):
        assert j == job
        return "done"

    monkeypatch.setattr(pipeline, "process_radar_job", fake_process)
    resp = client.post("/api/radar/jobs/R1/run")
    assert resp.status_code == 200
    assert resp.json() == {"reel_id": "R1", "status": "done"}


def test_jobs_tick_none_picked(monkeypatch):
    monkeypatch.setattr(pipeline, "claim_stale_job", lambda: None)
    resp = client.post("/api/radar/jobs/tick")
    assert resp.status_code == 200
    assert resp.json() == {"picked": None, "status": None}


def test_jobs_tick_picks_and_processes(monkeypatch):
    job = {"id": "job2", "reel_id": "R9", "mode": "v"}
    monkeypatch.setattr(pipeline, "claim_stale_job", lambda: job)

    async def fake_process(j):
        return "error"

    monkeypatch.setattr(pipeline, "process_radar_job", fake_process)
    resp = client.post("/api/radar/jobs/tick")
    assert resp.status_code == 200
    assert resp.json() == {"picked": "R9", "status": "error"}


# ── /analyze/cancel, /analyze/status ─────────────────────────────────────

def test_cancel_analyze_requires_ids():
    resp = client.post("/api/radar/analyze/cancel", json={"reel_ids": []})
    assert resp.status_code == 400


def test_cancel_analyze(monkeypatch):
    monkeypatch.setattr(repo, "cancel_jobs", lambda ids: ["R1"])
    resp = client.post("/api/radar/analyze/cancel", json={"reel_ids": ["R1", "R2"]})
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": ["R1"]}


def test_analyze_status_requires_ids():
    resp = client.get("/api/radar/analyze/status", params={"reel_ids": ""})
    assert resp.status_code == 400


def test_analyze_status_mixed_not_started(monkeypatch):
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {
        "R1": {"mode": "t", "status": "done", "error": None, "updated_at": "2026-09-24T00:00:00Z"},
    })
    resp = client.get("/api/radar/analyze/status", params={"reel_ids": "R1,R2"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["R1"]["status"] == "done"
    assert data["R2"] == {"reel_id": "R2", "mode": None, "status": "not_started", "error": None, "updated_at": None}


# ── /reels/{id}/analysis, /reels/{id}/report.md, /export.md ─────────────

def test_reel_analysis_not_found(monkeypatch):
    monkeypatch.setattr(repo, "get_reel", lambda reel_id: None)
    resp = client.get("/api/radar/reels/MISSING/analysis")
    assert resp.status_code == 404


def test_reel_analysis_found(monkeypatch):
    monkeypatch.setattr(repo, "get_reel", lambda reel_id: {"id": "R1", "username": "acc1"})
    monkeypatch.setattr(repo, "get_analysis", lambda reel_id: {"status": "done", "hook": "Хук"})
    resp = client.get("/api/radar/reels/R1/analysis")
    assert resp.status_code == 200
    assert resp.json() == {"reel": {"id": "R1", "username": "acc1"}, "analysis": {"status": "done", "hook": "Хук"}}


def test_report_md_not_found(monkeypatch):
    monkeypatch.setattr(repo, "get_reel", lambda reel_id: None)
    resp = client.get("/api/radar/reels/MISSING/report.md")
    assert resp.status_code == 404


def test_report_md_success(monkeypatch):
    reel = {
        "id": "R1", "username": "acc1", "views": 1000, "likes": 10, "comments": 2,
        "duration_sec": 15, "url": "https://instagram.com/reel/R1",
    }
    monkeypatch.setattr(repo, "get_reel", lambda reel_id: reel)
    monkeypatch.setattr(repo, "get_analysis", lambda reel_id: {"hook": "Хук!"})
    resp = client.get("/api/radar/reels/R1/report.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    assert "@acc1 — R1" in resp.text
    assert "Хук!" in resp.text


def test_export_md_requires_ids():
    resp = client.get("/api/radar/export.md", params={"reel_ids": ""})
    assert resp.status_code == 400


def test_export_md_joins_reports_and_skips_missing(monkeypatch):
    monkeypatch.setattr(repo, "get_reels_by_ids", lambda ids: {
        "R1": {"id": "R1", "username": "a", "views": 1, "likes": 0, "comments": 0, "duration_sec": 1, "url": "u1"},
        "R2": {"id": "R2", "username": "b", "views": 2, "likes": 0, "comments": 0, "duration_sec": 1, "url": "u2"},
    })
    monkeypatch.setattr(repo, "get_analyses_by_ids", lambda ids: {})
    resp = client.get("/api/radar/export.md", params={"reel_ids": "R1,R2,MISSING"})
    assert resp.status_code == 200
    assert "@a — R1" in resp.text
    assert "@b — R2" in resp.text
    assert "---" in resp.text
