from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.core.db import get_db
from app.pipeline.download import download_audio
from app.pipeline.metadata import extract_metadata
from app.pipeline.transcribe import transcribe_audio

logger = logging.getLogger(__name__)
AUDIO_DIR = Path(settings.audio_tmp_dir)
MAX_ATTEMPTS = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _session_flags(db, session_id: str) -> tuple[bool, bool]:
    """Возвращает (pull_stats, translate) для сессии."""
    if not session_id:
        return True, False
    sess = db.table('import_sessions').select('pull_stats, translate').eq('id', session_id).execute()
    row = sess.data[0] if sess.data else {}
    return row.get('pull_stats', True), row.get('translate', False)


def _maybe_close_session(db, session_id: str) -> None:
    """Помечает сессию как done, если все её джобы завершены."""
    if not session_id:
        return
    jobs = db.table('jobs').select('state').eq('session_id', session_id).execute()
    if not jobs.data:
        return
    pending = [j for j in jobs.data if j['state'] not in ('done', 'failed')]
    if not pending:
        db.table('import_sessions').update({'status': 'done'}).eq('id', session_id).execute()


async def _process_job(job: dict) -> None:
    db = get_db()
    job_id: str = job['id']
    reel_id: str = job['reel_id']
    session_id: str = job.get('session_id') or ''
    attempts: int = job.get('attempts', 0) + 1

    db.table('jobs').update({'state': 'in_progress', 'attempts': attempts}).eq('id', job_id).execute()
    db.table('transcripts').update({'status': 'downloading'}).eq('reel_id', reel_id).execute()

    try:
        reel_resp = db.table('reels').select('url').eq('id', reel_id).execute()
        url: str = reel_resp.data[0]['url']

        transcript_resp = (
            db.table('transcripts').select('engine, model').eq('reel_id', reel_id).execute()
        )
        model_name: str = (transcript_resp.data[0] if transcript_resp.data else {}).get('model') or 'medium'

        pull_stats, do_translate = _session_flags(db, session_id)

        # скачиваем аудио (throttle внутри download_audio)
        audio_path, info = await download_audio(url, AUDIO_DIR)

        # обновляем метаданные рилса
        if pull_stats:
            meta = extract_metadata(info)
            update = {k: v for k, v in meta.items() if v is not None}
            if update:
                db.table('reels').update(update).eq('id', reel_id).execute()

        # транскрибируем
        db.table('transcripts').update({'status': 'transcribing'}).eq('reel_id', reel_id).execute()
        text, language, duration_sec = await asyncio.to_thread(transcribe_audio, audio_path, model_name)

        # переводим на русский если запрошено и язык не русский
        text_ru: str | None = None
        if do_translate and text and language and language != 'ru':
            logger.info('Перевожу %s → ru…', language)
            db.table('transcripts').update({'status': 'translating'}).eq('reel_id', reel_id).execute()
            from app.pipeline.translate import translate_to_ru
            text_ru = await asyncio.to_thread(translate_to_ru, text)

        db.table('transcripts').update({
            'text': text,
            'text_ru': text_ru,
            'language': language,
            'duration_sec': int(duration_sec),
            'status': 'done',
            'fail_reason': None,
        }).eq('reel_id', reel_id).execute()

        db.table('jobs').update({'state': 'done', 'error': None}).eq('id', job_id).execute()
        logger.info('✓ %s (lang=%s, translate=%s)', url, language, do_translate)

    except Exception as exc:  # noqa: BLE001
        logger.error('✗ job %s: %s', job_id, exc)
        next_at = (datetime.utcnow() + timedelta(seconds=30 * attempts)).isoformat()
        state = 'failed' if attempts >= MAX_ATTEMPTS else 'queued'
        db.table('jobs').update({
            'state': state,
            'attempts': attempts,
            'next_attempt_at': next_at,
            'error': str(exc),
        }).eq('id', job_id).execute()
        db.table('transcripts').update({
            'status': 'failed',
            'fail_reason': str(exc)[:500],
        }).eq('reel_id', reel_id).execute()
    finally:
        # удаляем временные аудио-файлы
        try:
            for f in AUDIO_DIR.glob('*'):
                if f.is_file():
                    f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        _maybe_close_session(db, session_id)


async def run_worker() -> None:
    logger.info('Воркер запущен (AUDIO_DIR=%s)', AUDIO_DIR)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        db = get_db()
        now = _utc_now()
        jobs_resp = (
            db.table('jobs')
            .select('*')
            .eq('state', 'queued')
            .lte('next_attempt_at', now)
            .order('next_attempt_at')
            .limit(2)
            .execute()
        )
        jobs = jobs_resp.data
        if jobs:
            await asyncio.gather(*[_process_job(j) for j in jobs])
        else:
            await asyncio.sleep(2)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run_worker())
