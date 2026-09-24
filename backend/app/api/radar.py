"""Роутер /api/radar/* — раздел разведки по конкурентам (docs/specs/08-radar.md).

Тонкий роутер: валидация запроса + делегирование в app/radar/{scrape_service,analyze_service,
analytics,report,repo}.py. Разбор (`/jobs/*`) вызывает функции агента B из app.radar.pipeline —
импорт лениво, внутри обработчиков: тесты подменяют app.radar.pipeline в sys.modules.

Сверено с реализацией B (app/radar/pipeline.py, 2026-09-24): claim_job и claim_stale_job —
обычные синхронные функции (захват задания — один условный update, ждать нечего), а
process_radar_job и refresh_expired_links — корутины (внутри скачивание видео и вызов Gemini).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.models.radar_schemas import (
    AnalyzeCancelRequest,
    AnalyzeRequest,
    RefreshFollowersRequest,
    ScrapeRequest,
)
from app.radar import analytics, analyze_service, repo, report, scrape_service
from app.radar import settings as radar_settings

router = APIRouter(prefix="/radar", tags=["radar"])


# ── Конфигурация фронта ──────────────────────────────────────────────────

@router.get("/config")
async def get_config() -> dict[str, Any]:
    return {"whisper_available": bool(radar_settings.OPENAI_API_KEY)}


# ── Конкуренты ─────────────────────────────────────────────────────────────

@router.get("/competitors")
async def get_competitors() -> list[dict[str, Any]]:
    return repo.list_competitors()


@router.post("/competitors/refresh-followers")
async def refresh_followers(req: RefreshFollowersRequest) -> dict[str, Any]:
    try:
        run_id = await scrape_service.start_followers_scrape(req.usernames)
    except scrape_service.RadarValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except scrape_service.RadarHourlyLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except scrape_service.RadarApifyError as exc:
        raise HTTPException(502, f"Apify не ответил: {exc}") from exc
    return {"run_id": run_id}


# ── Сбор ───────────────────────────────────────────────────────────────────

@router.get("/scrape/cost-estimate")
async def scrape_cost_estimate(
    usernames: str = Query(""),
    period_months: int = Query(3),
) -> dict[str, Any]:
    names = scrape_service.normalize_usernames(usernames.split(","))
    return scrape_service.estimate_cost(names, period_months)


@router.post("/scrape")
async def start_scrape(req: ScrapeRequest) -> dict[str, Any]:
    try:
        run_id = await scrape_service.start_reel_scrape(req.usernames, req.period_months)
    except scrape_service.RadarValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except scrape_service.RadarHourlyLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except scrape_service.RadarApifyError as exc:
        raise HTTPException(502, f"Apify не ответил: {exc}") from exc
    return {"run_id": run_id}


@router.get("/scrape/{run_id}")
async def scrape_status(run_id: int) -> dict[str, Any]:
    run = await scrape_service.poll_scrape_run(run_id)
    if run is None:
        raise HTTPException(404, "Прогон не найден")
    return {
        "id": run["id"],
        "kind": run["kind"],
        "status": run["status"],
        "results_count": run.get("results_count"),
        "cost_estimate_usd": run.get("cost_estimate_usd"),
        "error": run.get("error"),
        "created_at": run.get("created_at"),
    }


# ── Аналитика ──────────────────────────────────────────────────────────────

@router.get("/summary")
async def summary(usernames: Optional[str] = Query(None)) -> dict[str, Any]:
    return analytics.get_summary(usernames)


@router.get("/reels")
async def get_reels(
    usernames: Optional[str] = Query(None),
    sort: str = Query("views"),
    order: str = Query("desc"),
    min_views: Optional[int] = Query(None),
    max_views: Optional[int] = Query(None),
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    return analytics.get_reels(
        usernames=usernames, sort=sort, order=order,
        min_views=min_views, max_views=max_views, limit=limit,
    )


# ── Разбор ─────────────────────────────────────────────────────────────────

@router.post("/analyze")
async def start_analyze(req: AnalyzeRequest) -> dict[str, Any]:
    if not req.items:
        raise HTTPException(400, "items required")

    try:
        return await analyze_service.enqueue_analysis(req.items, req.force, req.transcriber)
    except analyze_service.RadarReelNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except analyze_service.RadarAnalysisDailyLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except analyze_service.RadarWhisperUnavailableError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/jobs/{reel_id}/run")
async def run_job(reel_id: str) -> dict[str, Any]:
    from app.radar.pipeline import claim_job, process_radar_job

    job = claim_job(reel_id)
    if job is None:
        # Уже захвачено другим вызовом (или не queued) — отдаём текущий статус, не разбираем повторно.
        analysis = repo.get_analysis(reel_id)
        status = analysis["status"] if analysis else "not_started"
        return {"reel_id": reel_id, "status": status}

    status = await process_radar_job(job)
    return {"reel_id": reel_id, "status": status}


@router.post("/jobs/tick")
async def jobs_tick() -> dict[str, Any]:
    from app.radar.pipeline import claim_stale_job, process_radar_job

    job = claim_stale_job()
    if job is None:
        return {"picked": None, "status": None}

    status = await process_radar_job(job)
    return {"picked": job["reel_id"], "status": status}


@router.post("/analyze/cancel")
async def cancel_analyze(req: AnalyzeCancelRequest) -> dict[str, Any]:
    if not req.reel_ids:
        raise HTTPException(400, "reel_ids required")
    return {"cancelled": repo.cancel_jobs(req.reel_ids)}


@router.get("/analyze/status")
async def analyze_status(reel_ids: str = Query(...)) -> dict[str, Any]:
    ids = [r.strip() for r in reel_ids.split(",") if r.strip()]
    if not ids:
        raise HTTPException(400, "reel_ids required")

    rows = repo.get_analyses_by_ids(ids)
    result: dict[str, Any] = {}
    for reel_id in ids:
        row = rows.get(reel_id)
        if row:
            result[reel_id] = {
                "reel_id": reel_id,
                "mode": row.get("mode"),
                "status": row.get("status"),
                "error": row.get("error"),
                "updated_at": row.get("updated_at"),
            }
        else:
            result[reel_id] = {
                "reel_id": reel_id, "mode": None,
                "status": "not_started", "error": None, "updated_at": None,
            }
    return result


@router.get("/reels/{reel_id}/analysis")
async def reel_analysis(reel_id: str) -> dict[str, Any]:
    reel = repo.get_reel(reel_id)
    if reel is None:
        raise HTTPException(404, "Рилс не найден")
    return {"reel": reel, "analysis": repo.get_analysis(reel_id) or {}}


@router.get("/reels/{reel_id}/report.md")
async def reel_report_md(reel_id: str) -> Response:
    reel = repo.get_reel(reel_id)
    if reel is None:
        raise HTTPException(404, "Рилс не найден")
    md = report.build_report_md(reel, repo.get_analysis(reel_id))
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report_{reel_id}.md"'},
    )


@router.get("/export.md")
async def export_md(reel_ids: str = Query(...)) -> Response:
    ids = [r.strip() for r in reel_ids.split(",") if r.strip()]
    if not ids:
        raise HTTPException(400, "reel_ids required")

    reels = repo.get_reels_by_ids(ids)
    analyses = repo.get_analyses_by_ids(ids)
    parts = [report.build_report_md(reels[rid], analyses.get(rid)) for rid in ids if rid in reels]

    return Response(
        content="\n\n---\n\n".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reels_report.md"'},
    )
