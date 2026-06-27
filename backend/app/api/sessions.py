from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.core.db import get_db
from app.models.schemas import SessionCommentUpdate, SessionDetail, SessionSummary

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions() -> list[SessionSummary]:
    db = get_db()
    rows = db.table('import_sessions').select('*').order('created_at', desc=True).execute()
    return [SessionSummary(**r) for r in rows.data]


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID) -> SessionDetail:
    db = get_db()
    row = db.table('import_sessions').select('*').eq('id', str(session_id)).execute()
    if not row.data:
        raise HTTPException(404, "сессия не найдена")

    jobs = db.table('jobs').select('state, reel_id').eq('session_id', str(session_id)).execute()
    loaded = [j['reel_id'] for j in jobs.data if j['state'] == 'done']
    failed = sum(1 for j in jobs.data if j['state'] == 'failed')

    return SessionDetail(
        **row.data[0],
        loaded=len(loaded),
        failed=failed,
        done_ids=loaded,
    )


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
async def update_session(session_id: UUID, body: SessionCommentUpdate) -> SessionSummary:
    db = get_db()
    row = db.table('import_sessions').update({'comment': body.comment}).eq('id', str(session_id)).execute()
    if not row.data:
        raise HTTPException(404, "сессия не найдена")
    return SessionSummary(**row.data[0])
