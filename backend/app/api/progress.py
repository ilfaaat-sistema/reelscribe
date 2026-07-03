from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from app.core.db import get_db
from app.models.schemas import ProgressResponse

router = APIRouter(tags=["progress"])


@router.get("/progress", response_model=ProgressResponse)
async def get_progress(session: UUID = Query(...)) -> ProgressResponse:
    db = get_db()
    jobs = db.table('jobs').select('state, reel_id').eq('session_id', str(session)).execute()
    sess = db.table('import_sessions').select('total, status').eq('id', str(session)).execute()
    sess_row = sess.data[0] if sess.data else {}
    total = sess_row.get('total', 0)
    sess_status = sess_row.get('status', 'running')

    done_ids = [j['reel_id'] for j in jobs.data if j['state'] == 'done']
    failed_ids = [j['reel_id'] for j in jobs.data if j['state'] == 'failed']

    # Сессия закрыта, но джобов нет — все ссылки были из кэша
    if sess_status == 'done' and not jobs.data:
        loaded = total
        failed = 0
        done_ids = []
        failed_ids = []
    else:
        loaded = len(done_ids)
        failed = len(failed_ids)

    return ProgressResponse(
        session_id=session,
        loaded=loaded,
        total=total,
        failed=failed,
        done_ids=done_ids,
        failed_ids=failed_ids,
    )
