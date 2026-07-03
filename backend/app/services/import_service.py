from __future__ import annotations

import logging

from fastapi import BackgroundTasks, HTTPException

from app.core.config import settings
from app.core.db import get_db
from app.models.schemas import ImportRequest, ImportResponse
from app.pipeline.normalize import ParsedReel, parse_links, parse_message_json

logger = logging.getLogger(__name__)


def _done_shortcodes(shortcodes: list[str]) -> set[str]:
    if not shortcodes:
        return set()
    db = get_db()
    reels = db.table('reels').select('id, shortcode').in_('shortcode', shortcodes).execute()
    if not reels.data:
        return set()
    reel_ids = [r['id'] for r in reels.data]
    sc_by_id = {r['id']: r['shortcode'] for r in reels.data}
    done = (
        db.table('transcripts')
        .select('reel_id')
        .in_('reel_id', reel_ids)
        .eq('status', 'done')
        .execute()
    )
    return {sc_by_id[t['reel_id']] for t in done.data}


def _reel_id_by_shortcode(shortcodes: list[str]) -> dict[str, str]:
    if not shortcodes:
        return {}
    db = get_db()
    rows = db.table('reels').select('id, shortcode').in_('shortcode', shortcodes).execute()
    return {r['shortcode']: r['id'] for r in rows.data}


def _setup_jobs(
    session_id: str,
    unique: list[ParsedReel],
    req: ImportRequest,
) -> None:
    """Background: batch-create reel/transcript/job rows for this session."""
    try:
        done_set = _done_shortcodes([p.shortcode for p in unique])
        to_process = [p for p in unique if p.shortcode not in done_set]

        db = get_db()

        # Уже готовые рилсы привязываем к этой сессии «готовыми» джобами — без перекачки и
        # перераспознавания (соблюдаем дедуп). Так каждый импорт одного файла показывает ПОЛНЫЙ
        # список рилсов, а не пусто. total оставляем равным всему файлу (len(unique)).
        if done_set:
            done_ids = _reel_id_by_shortcode(list(done_set))
            if done_ids:
                db.table('jobs').insert([
                    {'reel_id': rid, 'session_id': session_id, 'state': 'done'}
                    for rid in done_ids.values()
                ]).execute()

        if not to_process:
            db.table('import_sessions').update({'status': 'done'}).eq('id', session_id).execute()
            return

        # 1 query: existing reels
        sc_to_id = _reel_id_by_shortcode([p.shortcode for p in to_process])

        # 1 query: batch insert new reels
        new_reels = [p for p in to_process if p.shortcode not in sc_to_id]
        if new_reels:
            inserted = db.table('reels').insert([
                {'shortcode': p.shortcode, 'url': p.url, 'type': p.type,
                 'first_session_id': session_id}
                for p in new_reels
            ]).execute()
            for r in inserted.data:
                sc_to_id[r['shortcode']] = r['id']

        all_reel_ids = list(sc_to_id.values())

        # 1 query: find reels that already have a transcript row
        existing_t = (
            db.table('transcripts').select('reel_id').in_('reel_id', all_reel_ids).execute()
        )
        existing_t_ids = {r['reel_id'] for r in existing_t.data}

        # 1 query: batch insert missing transcripts
        t_rows = [
            {'reel_id': rid, 'engine': req.engine, 'model': req.model, 'status': 'queued'}
            for rid in all_reel_ids if rid not in existing_t_ids
        ]
        if t_rows:
            db.table('transcripts').insert(t_rows).execute()

        # 1 query: batch insert jobs
        db.table('jobs').insert([
            {'reel_id': rid, 'session_id': session_id, 'state': 'queued'}
            for rid in all_reel_ids
        ]).execute()

        if req.model == 'large-v3':
            from app.pipeline.kaggle_trigger import trigger_kaggle_notebook
            if trigger_kaggle_notebook():
                logger.info('Kaggle notebook triggered for session %s', session_id)

    except Exception:
        # Синхронный вызов — исключение поднимется в роутер (видно клиенту), сессия не «зависнет»
        # молча. Статус 'failed' схема не разрешает (только running/done), поэтому просто логируем и
        # пробрасываем.
        logger.exception('Job setup failed for session %s', session_id)
        raise


async def handle_import(
    req: ImportRequest,
    file_bytes: bytes | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> ImportResponse:
    if file_bytes is not None and req.source_type == 'json':
        parsed = parse_message_json(file_bytes)
    else:
        parsed = parse_links(req.links_text)

    seen: dict[str, ParsedReel] = {p.shortcode: p for p in parsed}
    unique = list(seen.values())
    duplicates_in_batch = len(parsed) - len(unique)

    # Защита кошелька: сервис публичный и без auth, каждый импорт тратит платные
    # API-квоты владельца — режем слишком жирные партии.
    if len(unique) > settings.import_max_links:
        raise HTTPException(
            status_code=413,
            detail=f'Слишком много ссылок: {len(unique)}. '
                   f'Максимум {settings.import_max_links} за один импорт — разбей на части.',
        )

    reels_count = sum(1 for p in unique if p.type == 'reel')
    posts_count = len(unique) - reels_count

    db = get_db()

    # 1 query: create session → return immediately
    session_resp = db.table('import_sessions').insert({
        'source_type': req.source_type,
        'engine': req.engine,
        'model': req.model,
        'translate': req.translate,
        'pull_stats': req.pull_stats,
        'comment': req.comment,
        'total': len(unique),
        'status': 'running',
    }).execute()
    session_id: str = session_resp.data[0]['id']

    # Нарезаем reel/transcript/job СИНХРОННО: это быстрые батч-вставки (~1-2с на сотни),
    # а fire-and-forget BackgroundTask тихо гибнет (uvicorn --reload, потерянные исключения)
    # и оставляет сессию «running» с 0 джобов.
    _setup_jobs(session_id, unique, req)

    return ImportResponse(
        session_id=session_id,
        total=len(unique),
        reels_count=reels_count,
        posts_count=posts_count,
        duplicates=duplicates_in_batch,
    )
