"""Разбор рилса: захват задания из очереди `radar_jobs` и его выполнение.

Выполняется ВНУТРИ HTTP-запроса Vercel serverless (лимит 300 с) — никаких фоновых
потоков/воркеров. Вызывается из роутера агента A (`app/api/radar.py`):
`POST /jobs/{reel_id}/run` → `claim_job` + `process_radar_job`;
`POST /jobs/tick` → `claim_stale_job` + `process_radar_job` (страховка pg_cron);
`POST /analyze` → `refresh_expired_links` (заранее, пачкой, до постановки заданий).

Доступ к БД — напрямую через `app.core.db.get_db()`, без зависимости от `radar/repo.py`
агента A (он пишется параллельно тем же ТЗ).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone

from app.core.db import get_db
from app.radar import downloader, gemini, settings

logger = logging.getLogger(__name__)

# Сколько кандидатов пробуем на захват за один вызов claim_* — не единица, чтобы
# случайный проигрыш гонки за первую строку (её увёл параллельный запрос) не оставлял
# claim_stale_job() безрезультатным на пустом месте.
_CLAIM_CANDIDATES = 3

_RATE_LIMIT_RETRY_SEC = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ── Захват заданий ──────────────────────────────────────────────────────────────────────


def claim_job(reel_id: str) -> dict | None:
    """Атомарно `queued → in_progress` для последнего задания указанного рилса. `attempts+1`.

    Условный update по `state='queued'` (и уже наступившему `next_attempt_at`) —гарантия
    от supabase-py/PostgREST: если два запроса `/jobs/{reel_id}/run` пришли одновременно,
    только один из них найдёт строку в состоянии `queued` и обновит её; второй получит 0
    затронутых строк и вернёт `None`.

    Возвращает `None`, если задания нет, оно не `queued`, `next_attempt_at` ещё не
    наступил (отложенный повтор после rate-limit), либо его успел забрать параллельный
    запрос.
    """
    db = get_db()
    now_iso = _now_iso()

    candidates = (
        db.table("radar_jobs")
        .select("*")
        .eq("reel_id", reel_id)
        .eq("state", "queued")
        .lte("next_attempt_at", now_iso)
        .order("created_at")
        .limit(_CLAIM_CANDIDATES)
        .execute()
    ).data or []

    for cand in candidates:
        claimed = _try_claim_queued(db, cand, now_iso)
        if claimed:
            return claimed
    return None


def claim_stale_job() -> dict | None:
    """Страховка `pg_cron` (раз в минуту, `POST /jobs/tick`): одно зависшее задание.

    Два независимых случая, каждый максимум на одно возвращаемое задание за вызов:
    - `queued`, которое никто не взял за `STALE_QUEUED_SEC` (закрыли вкладку сразу
      после `/analyze`) и чей `next_attempt_at` уже наступил — забираем как обычный
      захват (`attempts+1`).
    - `in_progress` дольше `STALE_RUNNING_SEC` (функцию убил лимит выполнения Vercel):
      `attempts+1`; после `MAX_ATTEMPTS` попыток задание не переоткрываем — сразу
      помечаем `radar_jobs.state='failed'` и `radar_analyses.status='error'`, ищем
      следующего кандидата, ничего не возвращая на обработку в этот проход.
    """
    db = get_db()
    now = _now()
    now_iso = now.isoformat()

    queued_threshold = (now - timedelta(seconds=settings.STALE_QUEUED_SEC)).isoformat()
    stuck_queued = (
        db.table("radar_jobs")
        .select("*")
        .eq("state", "queued")
        .lte("next_attempt_at", now_iso)
        .lt("created_at", queued_threshold)
        .order("created_at")
        .limit(_CLAIM_CANDIDATES)
        .execute()
    ).data or []
    for cand in stuck_queued:
        claimed = _try_claim_queued(db, cand, now_iso)
        if claimed:
            return claimed

    running_threshold = (now - timedelta(seconds=settings.STALE_RUNNING_SEC)).isoformat()
    stuck_running = (
        db.table("radar_jobs")
        .select("*")
        .eq("state", "in_progress")
        .lt("updated_at", running_threshold)
        .order("updated_at")
        .limit(_CLAIM_CANDIDATES)
        .execute()
    ).data or []
    for cand in stuck_running:
        attempts = (cand.get("attempts") or 0) + 1
        if attempts > settings.MAX_ATTEMPTS:
            _fail_stuck_job(db, cand, attempts, running_threshold, now_iso)
            continue

        resp = (
            db.table("radar_jobs")
            .update({"state": "in_progress", "attempts": attempts, "updated_at": now_iso})
            .eq("id", cand["id"])
            .eq("state", "in_progress")
            .lt("updated_at", running_threshold)
            .execute()
        )
        rows = resp.data or []
        if rows:
            return rows[0]

    return None


def _try_claim_queued(db, cand: dict, now_iso: str) -> dict | None:
    attempts = (cand.get("attempts") or 0) + 1
    resp = (
        db.table("radar_jobs")
        .update({"state": "in_progress", "attempts": attempts, "updated_at": now_iso, "error": None})
        .eq("id", cand["id"])
        .eq("state", "queued")
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _fail_stuck_job(db, cand: dict, attempts: int, running_threshold: str, now_iso: str) -> None:
    message = "Превышено число попыток разбора"
    resp = (
        db.table("radar_jobs")
        .update({"state": "failed", "attempts": attempts, "updated_at": now_iso, "error": message})
        .eq("id", cand["id"])
        .eq("state", "in_progress")
        .lt("updated_at", running_threshold)
        .execute()
    )
    if resp.data:
        _set_analysis_status(db, cand["reel_id"], cand.get("mode", "tv"), "error", error=message)
        logger.warning("Задание %s (рилс %s) помечено failed после %d попыток", cand["id"], cand["reel_id"], attempts)


# ── Обработка захваченного задания ──────────────────────────────────────────────────────


async def process_radar_job(job: dict) -> str:
    """Выполняет одно захваченное задание. Возвращает итоговый статус `radar_analyses`.

    Шаги: `downloading` → скачивание → `analyzing` → `gemini.analyze` → запись результата.
    Отмена (`radar_jobs.state='cancelled'`) проверяется между шагами. Любая ошибка
    записывается человеческим текстом в `radar_analyses.error` и `radar_jobs.error`.
    Временная папка с mp4 удаляется всегда (контекстный менеджер `TemporaryDirectory`),
    даже при ошибке или отмене.
    """
    db = get_db()
    job_id = job["id"]
    reel_id = job["reel_id"]
    mode = job.get("mode") or "tv"

    if _job_cancelled(db, job_id):
        return _finish_cancelled(db, job_id, reel_id, mode)

    _set_analysis_status(db, reel_id, mode, "downloading")

    reel = _get_reel(db, reel_id)
    if not reel:
        return _finish_error(db, job_id, reel_id, mode, "Рилс не найден в базе")

    try:
        with tempfile.TemporaryDirectory(prefix="radar_") as tmpdir:
            try:
                video_path = await downloader.download_to(tmpdir, reel)
            except downloader.DownloadError as e:
                return _finish_error(db, job_id, reel_id, mode, str(e))

            if _job_cancelled(db, job_id):
                return _finish_cancelled(db, job_id, reel_id, mode)

            _set_analysis_status(db, reel_id, mode, "analyzing")

            try:
                # Вызов Gemini синхронный и длится 30–60 с — уводим из цикла событий, иначе
                # на том же экземпляре Vercel зависнут опросы /analyze/status
                result = await asyncio.to_thread(
                    gemini.analyze, video_path, mode, reel.get("duration_sec")
                )
            except gemini.RateLimitedError as e:
                return _finish_rate_limited(db, job_id, reel_id, mode, str(e))
            except Exception as e:  # noqa: BLE001 — любая другая ошибка Gemini/валидации таймкодов
                return _finish_error(db, job_id, reel_id, mode, _human_error(e))
    except Exception as e:  # подстраховка: TemporaryDirectory удалится в любом случае
        logger.exception("process_radar_job: неожиданная ошибка для рилса %s", reel_id)
        return _finish_error(db, job_id, reel_id, mode, _human_error(e))

    if _job_cancelled(db, job_id):
        return _finish_cancelled(db, job_id, reel_id, mode)

    return _finish_done(db, job_id, reel_id, mode, result)


def _human_error(e: Exception) -> str:
    if isinstance(e, gemini.ValidationError):
        return f"Gemini вернул некорректные таймкоды даже после повторной попытки: {e}"
    return f"Не удалось разобрать рилс: {e}"


def _job_cancelled(db, job_id: str) -> bool:
    rows = (
        db.table("radar_jobs").select("state").eq("id", job_id).limit(1).execute()
    ).data or []
    return bool(rows) and rows[0].get("state") == "cancelled"


def _get_reel(db, reel_id: str) -> dict | None:
    rows = (
        db.table("radar_reels").select("*").eq("id", reel_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _set_analysis_status(db, reel_id: str, mode: str, status: str, error: str | None = None) -> None:
    """Upsert по `reel_id` — не зависим от того, создал ли уже строку `radar_analyses`
    агент A при постановке в очередь (ТЗ явно требует не полагаться на его `repo.py`)."""
    db.table("radar_analyses").upsert(
        {"reel_id": reel_id, "mode": mode, "status": status, "error": error, "updated_at": _now_iso()},
        on_conflict="reel_id",
    ).execute()


def _finish_done(db, job_id: str, reel_id: str, mode: str, result: dict) -> str:
    now_iso = _now_iso()
    payload = {
        "reel_id": reel_id,
        "mode": mode,
        "status": "done",
        "error": None,
        "updated_at": now_iso,
        "transcript_segments": result.get("transcript_segments"),
        "transcript_language": result.get("transcript_language"),
        "transcript_engine": "gemini" if mode in ("t", "tv") else None,
        "visual_timeline": result.get("visual_timeline"),
        "hook": result.get("hook"),
        "structure": result.get("structure"),
        "cta": result.get("cta"),
        "format_idea": result.get("format_idea"),
    }
    db.table("radar_analyses").upsert(payload, on_conflict="reel_id").execute()
    db.table("radar_jobs").update(
        {"state": "done", "error": None, "updated_at": now_iso}
    ).eq("id", job_id).execute()
    return "done"


def _finish_error(db, job_id: str, reel_id: str, mode: str, message: str) -> str:
    now_iso = _now_iso()
    _set_analysis_status(db, reel_id, mode, "error", error=message)
    db.table("radar_jobs").update(
        {"state": "failed", "error": message, "updated_at": now_iso}
    ).eq("id", job_id).execute()
    return "error"


def _finish_rate_limited(db, job_id: str, reel_id: str, mode: str, message: str) -> str:
    now = _now()
    next_attempt = (now + timedelta(seconds=_RATE_LIMIT_RETRY_SEC)).isoformat()
    _set_analysis_status(db, reel_id, mode, "rate_limited", error=message)
    db.table("radar_jobs").update(
        {
            "state": "queued",
            "next_attempt_at": next_attempt,
            "error": message,
            "updated_at": now.isoformat(),
        }
    ).eq("id", job_id).execute()
    return "rate_limited"


def _finish_cancelled(db, job_id: str, reel_id: str, mode: str) -> str:
    now_iso = _now_iso()
    _set_analysis_status(db, reel_id, mode, "cancelled", error=None)
    db.table("radar_jobs").update(
        {"state": "cancelled", "updated_at": now_iso}
    ).eq("id", job_id).execute()
    return "cancelled"


# ── Освежение протухших ссылок перед постановкой заданий ───────────────────────────────


async def refresh_expired_links(reel_ids: list[str]) -> None:
    """Освежает `video_url` рилсов с протухшим `oe` ОДНИМ вызовом Apify. Вызывается из
    `POST /analyze` (агент A) заранее, пачкой — до постановки заданий в очередь, чтобы к
    моменту запуска `/jobs/{id}/run` ссылки уже были живыми и не тратить на это отдельный
    вызов внутри каждого задания."""
    if not reel_ids:
        return

    db = get_db()
    rows = (
        db.table("radar_reels").select("id,video_url").in_("id", reel_ids).execute()
    ).data or []
    expired = [r["id"] for r in rows if downloader.link_expired(r.get("video_url"))]
    if not expired:
        return

    await downloader.refresh_video_urls(expired)
