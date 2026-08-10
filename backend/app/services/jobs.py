from __future__ import annotations

from datetime import datetime, timezone

from app.core.db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def requeue_jobs(session_id: str | None = None) -> int:
    """Сбросить незавершённые джобы (failed + queued) обратно в работу.

    Джобы → state='queued', next_attempt_at=now, attempts=0, error=null.
    Транскрипты (кроме уже 'done') → status='queued', fail_reason=null.
    Если задан session_id — сессия снова 'running'. Возвращает число джобов.
    """
    db = get_db()
    q = db.table('jobs').select('id, reel_id').in_('state', ['failed', 'queued'])
    if session_id:
        q = q.eq('session_id', session_id)
    rows = q.execute().data
    if not rows:
        return 0

    # Исключаем терминальные "нет аудио" (фото/карусель) — ретраить бессмысленно: NoAudioError
    # прилетит опять. Обычно пишется status='no_audio', но если в БД CHECK-ограничение на
    # transcripts.status ещё не пускает 'no_audio', run.py фолбэком пишет status='failed' с ТЕМ ЖЕ
    # fail_reason — тогда единственный признак терминальности это сам текст причины, а не статус.
    reel_ids_all = list({r['reel_id'] for r in rows})
    na = (
        db.table('transcripts').select('reel_id')
        .in_('reel_id', reel_ids_all)
        .or_("status.eq.no_audio,fail_reason.eq.фото/карусель — нет аудио")
        .execute().data
    )
    na_set = {t['reel_id'] for t in na}
    rows = [r for r in rows if r['reel_id'] not in na_set]
    if not rows:
        return 0

    job_ids = [r['id'] for r in rows]
    reel_ids = list({r['reel_id'] for r in rows})

    db.table('jobs').update(
        {'state': 'queued', 'next_attempt_at': _now(), 'attempts': 0, 'error': None}
    ).in_('id', job_ids).execute()

    # Не трогаем уже расшифрованные рилсы (могли быть готовы в другой сессии).
    db.table('transcripts').update(
        {'status': 'queued', 'fail_reason': None}
    ).in_('reel_id', reel_ids).neq('status', 'done').execute()

    if session_id:
        db.table('import_sessions').update({'status': 'running'}).eq('id', session_id).execute()

    return len(job_ids)
