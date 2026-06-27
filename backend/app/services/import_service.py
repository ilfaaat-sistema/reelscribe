from __future__ import annotations

from app.core.db import get_db
from app.models.schemas import ImportRequest, ImportResponse
from app.pipeline.normalize import ParsedReel, parse_links, parse_message_json


def _done_shortcodes(shortcodes: list[str]) -> set[str]:
    if not shortcodes:
        return set()
    db = get_db()
    reels = db.table('reels').select('id, shortcode').in_('shortcode', shortcodes).execute()
    if not reels.data:
        return set()
    reel_ids = [r['id'] for r in reels.data]
    sc_by_id = {r['id']: r['shortcode'] for r in reels.data}
    done_transcripts = (
        db.table('transcripts')
        .select('reel_id')
        .in_('reel_id', reel_ids)
        .eq('status', 'done')
        .execute()
    )
    return {sc_by_id[t['reel_id']] for t in done_transcripts.data}


def _reel_id_by_shortcode(shortcodes: list[str]) -> dict[str, str]:
    if not shortcodes:
        return {}
    db = get_db()
    rows = db.table('reels').select('id, shortcode').in_('shortcode', shortcodes).execute()
    return {r['shortcode']: r['id'] for r in rows.data}


async def handle_import(
    req: ImportRequest,
    file_bytes: bytes | None = None,
) -> ImportResponse:
    if file_bytes is not None and req.source_type == 'json':
        parsed = parse_message_json(file_bytes)
    else:
        parsed = parse_links(req.links_text)

    seen: dict[str, ParsedReel] = {p.shortcode: p for p in parsed}
    unique = list(seen.values())
    duplicates_in_batch = len(parsed) - len(unique)

    done_set = _done_shortcodes([p.shortcode for p in unique])
    existing_ids = _reel_id_by_shortcode([p.shortcode for p in unique])

    db = get_db()

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

    reels_count = posts_count = 0

    for p in unique:
        if p.type == 'reel':
            reels_count += 1
        else:
            posts_count += 1

        if p.shortcode in done_set:
            continue   # кэш — не обрабатываем повторно

        reel_id = existing_ids.get(p.shortcode)
        if reel_id is None:
            reel_resp = db.table('reels').insert({
                'shortcode': p.shortcode,
                'url': p.url,
                'type': p.type,
                'first_session_id': session_id,
            }).execute()
            reel_id = reel_resp.data[0]['id']

        existing_t = (
            db.table('transcripts').select('id').eq('reel_id', reel_id).execute()
        )
        if not existing_t.data:
            db.table('transcripts').insert({
                'reel_id': reel_id,
                'engine': req.engine,
                'model': req.model,
                'status': 'queued',
            }).execute()

        db.table('jobs').insert({
            'reel_id': reel_id,
            'session_id': session_id,
            'state': 'queued',
        }).execute()

    return ImportResponse(
        session_id=session_id,
        total=len(unique),
        reels_count=reels_count,
        posts_count=posts_count,
        duplicates=duplicates_in_batch + len(done_set),
    )
