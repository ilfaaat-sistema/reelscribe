from __future__ import annotations

import re
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.core.db import get_db
from app.models.schemas import NoteUpdate, ReelDetail, ReelListResponse, ReelRow

router = APIRouter(tags=["reels"])

PAGE_SIZE = 50

_SORT_MAP = {
    'created_at': 'created_at',
    'views': 'views',
    'er': 'er',
    'likes': 'likes',
    'comments': 'comments',
    'author_followers': 'author_followers',
    'posted_at': 'posted_at',
    'lpf': 'lpf',
    'cpf': 'cpf',
    'eng': 'eng',
}


def _build_row(r: dict, note_ids: set[str]) -> ReelRow:
    t = (r.get('transcripts') or [{}])[0]
    return ReelRow(
        id=r['id'],
        shortcode=r['shortcode'],
        url=r['url'],
        type=r['type'],
        caption=r.get('caption'),
        author_handle=r.get('author_handle'),
        author_followers=r.get('author_followers'),
        views=r.get('views'),
        likes=r.get('likes'),
        comments=r.get('comments'),
        posted_at=r.get('posted_at'),
        er=r.get('er'),
        lpf=r.get('lpf'),
        cpf=r.get('cpf'),
        eng=r.get('eng'),
        transcript_status=t.get('status'),
        transcript_text=t.get('text'),
        transcript_text_ru=t.get('text_ru'),
        has_note=r['id'] in note_ids,
    )


def _search_reel_ids(db, q: str) -> set[str]:
    """id рилсов, где q встречается в caption ИЛИ в тексте расшифровки (по всей базе).

    PostgREST не умеет OR между родительской и вложенной таблицей, поэтому два
    точечных запроса за id + объединение — вместо фильтрации страницы в Python.
    """
    safe = re.sub(r'[,()*%]', ' ', q).strip()
    if not safe:
        return set()
    cap = db.table('reels').select('id').ilike('caption', f'%{safe}%').execute()
    tr = (
        db.table('transcripts')
        .select('reel_id')
        .or_(f'text.ilike.*{safe}*,text_ru.ilike.*{safe}*')
        .execute()
    )
    return {r['id'] for r in cap.data} | {r['reel_id'] for r in tr.data}


@router.get("/reels", response_model=ReelListResponse)
async def list_reels(
    session: Optional[UUID] = Query(None),
    q: Optional[str] = Query(None),
    filter: Annotated[str, Query()] = "all",  # noqa: A002
    author: Optional[str] = Query(None),
    sort: Annotated[str, Query()] = "created_at",
    direction: Annotated[str, Query(alias="dir")] = "desc",
    min_views: Optional[int] = Query(None),
    min_likes: Optional[int] = Query(None),
    min_comments: Optional[int] = Query(None),
    min_followers: Optional[int] = Query(None),
    min_er: Optional[float] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(PAGE_SIZE, ge=1, le=500),
) -> ReelListResponse:
    db = get_db()

    # done/failed фильтруем на стороне БД: !inner превращает вложенный select в JOIN,
    # и .eq по transcripts.status отсекает родительские строки (а не только вложенные).
    if filter in ('done', 'failed'):
        select = '*, transcripts!inner(status, text, text_ru)'
    else:
        select = '*, transcripts(status, text, text_ru)'
    query = db.table('reels').select(select, count='exact')
    if filter == 'done':
        query = query.eq('transcripts.status', 'done')
    elif filter == 'failed':
        query = query.eq('transcripts.status', 'failed')

    if session:
        job_rows = db.table('jobs').select('reel_id').eq('session_id', str(session)).execute()
        ids = [j['reel_id'] for j in job_rows.data]
        if not ids:
            return ReelListResponse(items=[], total=0)
        query = query.in_('id', ids)

    if q:
        match_ids = _search_reel_ids(db, q)
        if not match_ids:
            return ReelListResponse(items=[], total=0)
        query = query.in_('id', list(match_ids))

    if author:
        query = query.eq('author_handle', author)
    if min_views is not None:
        query = query.gte('views', min_views)
    if min_likes is not None:
        query = query.gte('likes', min_likes)
    if min_comments is not None:
        query = query.gte('comments', min_comments)
    if min_followers is not None:
        query = query.gte('author_followers', min_followers)
    if min_er is not None:
        query = query.gte('er', min_er)
    if filter == 'viral':
        query = query.gte('er', 5)

    col = _SORT_MAP.get(sort, 'created_at')
    desc = direction.lower() != 'asc'
    query = (
        query
        .order(col, desc=desc, nullsfirst=False)
        .range((page - 1) * limit, page * limit - 1)
    )

    rows = query.execute()

    page_ids = [r['id'] for r in rows.data]
    note_ids: set[str] = set()
    if page_ids:
        note_ids = {
            r['reel_id']
            for r in db.table('reel_notes').select('reel_id').in_('reel_id', page_ids).execute().data
        }

    items = [_build_row(r, note_ids) for r in rows.data]
    return ReelListResponse(items=items, total=rows.count or 0)


@router.get("/reels/{reel_id}", response_model=ReelDetail)
async def get_reel(reel_id: UUID) -> ReelDetail:
    db = get_db()
    row = (
        db.table('reels')
        .select('*, transcripts(*), reel_notes(note)')
        .eq('id', str(reel_id))
        .execute()
    )
    if not row.data:
        raise HTTPException(404, "рилс не найден")
    r = row.data[0]
    t = (r.get('transcripts') or [{}])[0]
    note_list = r.get('reel_notes') or []
    note = note_list[0].get('note') if note_list else None

    return ReelDetail(
        id=r['id'],
        shortcode=r['shortcode'],
        url=r['url'],
        type=r['type'],
        caption=r.get('caption'),
        author_handle=r.get('author_handle'),
        author_followers=r.get('author_followers'),
        views=r.get('views'),
        likes=r.get('likes'),
        comments=r.get('comments'),
        posted_at=r.get('posted_at'),
        er=r.get('er'),
        lpf=r.get('lpf'),
        cpf=r.get('cpf'),
        eng=r.get('eng'),
        transcript_status=t.get('status'),
        transcript_text=t.get('text'),
        transcript_text_ru=t.get('text_ru'),
        has_note=bool(note),
        transcript_language=t.get('language'),
        transcript_duration_sec=t.get('duration_sec'),
        summary=t.get('summary'),
        tags=t.get('tags'),
        note=note,
        fail_reason=t.get('fail_reason'),
    )


@router.patch("/reels/{reel_id}/note")
async def update_note(reel_id: UUID, body: NoteUpdate) -> dict:
    db = get_db()
    db.table('reel_notes').upsert({'reel_id': str(reel_id), 'note': body.note}).execute()
    return {'ok': True}


@router.post("/reels/{reel_id}/summary")
async def generate_summary(reel_id: UUID) -> dict:
    raise HTTPException(501, "саммари ещё не реализовано")
