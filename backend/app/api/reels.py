from __future__ import annotations

import re
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.core.db import get_db
from app.models.schemas import (
    NoteUpdate,
    ReelDetail,
    ReelListResponse,
    ReelRow,
    SourcesResponse,
    SourceStat,
)

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


def _sources_agg(sources: list[dict]) -> tuple[list[str], list[str], bool]:
    """Из embed'а reel_sources собирает уникальные учётки, папки и флаг «есть в директе»."""
    accounts = sorted({s['account'] for s in sources if s.get('account')})
    folders = sorted({s['name'] for s in sources if s.get('kind') == 'folder' and s.get('name')})
    from_direct = any(s.get('kind') == 'direct' for s in sources)
    return accounts, folders, from_direct


def _build_row(r: dict, note_ids: set[str]) -> ReelRow:
    t = (r.get('transcripts') or [{}])[0]
    accounts, folders, from_direct = _sources_agg(r.get('reel_sources') or [])
    return ReelRow(
        id=r['id'],
        shortcode=r['shortcode'],
        url=r['url'],
        type=r['type'],
        caption=r.get('caption'),
        caption_ru=r.get('caption_ru'),
        caption_lang=r.get('caption_lang'),
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
        transcript_language=t.get('language'),
        has_note=r['id'] in note_ids,
        accounts=accounts,
        folders=folders,
        from_direct=from_direct,
    )


def _search_reel_ids(db, q: str) -> set[str]:
    """id рилсов, где q встречается в caption/caption_ru ИЛИ в тексте расшифровки (по всей базе).

    PostgREST не умеет OR между родительской и вложенной таблицей, поэтому два
    точечных запроса за id + объединение — вместо фильтрации страницы в Python.
    """
    safe = re.sub(r'[,()*%]', ' ', q).strip()
    if not safe:
        return set()
    cap = (
        db.table('reels')
        .select('id')
        .or_(f'caption.ilike.*{safe}*,caption_ru.ilike.*{safe}*')
        .execute()
    )
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
    account: Optional[str] = Query(None),
    folder: Optional[str] = Query(None),
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
        transcripts_part = 'transcripts!inner(status, text, text_ru, language)'
    else:
        transcripts_part = 'transcripts(status, text, text_ru, language)'
    # account/folder — тот же приём: !inner на reel_sources, чтобы отфильтровать
    # родительские строки прямо на стороне PostgREST (без ID-lookup, id тысячи).
    if account or folder:
        sources_part = 'reel_sources!inner(account, kind, name)'
    else:
        sources_part = 'reel_sources(account, kind, name)'
    select = f'*, {transcripts_part}, {sources_part}'
    query = db.table('reels').select(select, count='exact')
    if filter == 'done':
        query = query.eq('transcripts.status', 'done')
    elif filter == 'failed':
        query = query.eq('transcripts.status', 'failed')
    if account:
        query = query.eq('reel_sources.account', account)
    if folder:
        query = query.eq('reel_sources.name', folder.strip())

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
        .select('*, transcripts(*), reel_notes(note), reel_sources(account, kind, name)')
        .eq('id', str(reel_id))
        .execute()
    )
    if not row.data:
        raise HTTPException(404, "рилс не найден")
    r = row.data[0]
    t = (r.get('transcripts') or [{}])[0]
    note_list = r.get('reel_notes') or []
    note = note_list[0].get('note') if note_list else None
    accounts, folders, from_direct = _sources_agg(r.get('reel_sources') or [])

    return ReelDetail(
        id=r['id'],
        shortcode=r['shortcode'],
        url=r['url'],
        type=r['type'],
        caption=r.get('caption'),
        caption_ru=r.get('caption_ru'),
        caption_lang=r.get('caption_lang'),
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
        accounts=accounts,
        folders=folders,
        from_direct=from_direct,
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


# Путь "/sources" не пересекается с "/reels/{reel_id}" (разные корневые сегменты),
# поэтому порядок регистрации в роутере не важен.
@router.get("/sources", response_model=SourcesResponse)
async def list_sources() -> SourcesResponse:
    db = get_db()
    rows = db.rpc('reel_sources_stats', {}).execute()
    items = [SourceStat(**row) for row in rows.data]
    return SourcesResponse(items=items)
