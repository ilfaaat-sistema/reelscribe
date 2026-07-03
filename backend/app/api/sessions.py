from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.core.db import get_db
from app.models.schemas import SessionCommentUpdate, SessionDetail, SessionSummary
from app.services.jobs import requeue_jobs

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions() -> list[SessionSummary]:
    db = get_db()
    rows = db.table('import_sessions').select('*').order('created_at', desc=True).execute()
    if not rows.data:
        return []

    # Один доп. запрос на все сессии: считаем done/failed/queued по таблице jobs (без N+1).
    session_ids = [r['id'] for r in rows.data]
    jobs = (
        db.table('jobs')
        .select('session_id, state')
        .in_('session_id', session_ids)
        .execute()
    )
    counts: dict[str, dict[str, int]] = {}
    for j in jobs.data:
        c = counts.setdefault(j['session_id'], {'loaded': 0, 'failed': 0, 'queued': 0})
        if j['state'] == 'done':
            c['loaded'] += 1
        elif j['state'] == 'failed':
            c['failed'] += 1
        elif j['state'] == 'queued':
            c['queued'] += 1

    return [SessionSummary(**r, **counts.get(r['id'], {})) for r in rows.data]


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID) -> SessionDetail:
    db = get_db()
    row = db.table('import_sessions').select('*').eq('id', str(session_id)).execute()
    if not row.data:
        raise HTTPException(404, "сессия не найдена")

    jobs = db.table('jobs').select('state, reel_id').eq('session_id', str(session_id)).execute()
    loaded = [j['reel_id'] for j in jobs.data if j['state'] == 'done']
    failed = sum(1 for j in jobs.data if j['state'] == 'failed')
    queued = sum(1 for j in jobs.data if j['state'] == 'queued')

    return SessionDetail(
        **row.data[0],
        loaded=len(loaded),
        failed=failed,
        queued=queued,
        done_ids=loaded,
    )


@router.post("/retry")
async def retry_jobs(session: Optional[UUID] = Query(None)) -> dict:
    """Повторить незавершённые рилсы: всех (session=None) или одной сессии."""
    n = requeue_jobs(str(session) if session else None)
    return {"requeued": n}


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
async def update_session(session_id: UUID, body: SessionCommentUpdate) -> SessionSummary:
    db = get_db()
    row = db.table('import_sessions').update({'comment': body.comment}).eq('id', str(session_id)).execute()
    if not row.data:
        raise HTTPException(404, "сессия не найдена")
    return SessionSummary(**row.data[0])
