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
    sess = db.table('import_sessions').select('total').eq('id', str(session)).execute()
    total = sess.data[0].get('total', 0) if sess.data else 0
    done_ids = [j['reel_id'] for j in jobs.data if j['state'] == 'done']
    failed = sum(1 for j in jobs.data if j['state'] == 'failed')
    return ProgressResponse(
        session_id=session,
        loaded=len(done_ids),
        total=total,
        failed=failed,
        done_ids=done_ids,
    )
