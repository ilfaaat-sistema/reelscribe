from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.core.db import get_db
from app.pipeline.apify_profile_downloader import NoAudioError
from app.pipeline.download import download_audio
from app.pipeline.starapi_downloader import QuotaExceededError
from app.pipeline.metadata import extract_metadata
from app.pipeline.transcribe import transcribe_audio

logger = logging.getLogger(__name__)
AUDIO_DIR = Path(settings.audio_tmp_dir)
MAX_ATTEMPTS = 3
MAX_QUOTA_DEFERRALS = 16      # ~8ч ожидания квоты при cooldown 30 мин, потом failed
STALE_JOB_MINUTES = 45        # in_progress старше этого — считается брошенным (воркер умер)
STALE_CHECK_INTERVAL_SEC = 300

# Limit concurrent transcriptions to avoid OOM on M1 8GB
_transcription_sem = asyncio.Semaphore(1)  # 1 транскрипция за раз — экономия RAM на M1 8GB


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_now() -> str:
    return _utc_now_dt().isoformat()


def _quota_deferrals(error: str | None) -> int:
    if error and error.startswith('quota_deferred:'):
        try:
            return int(error.split(':', 1)[1])
        except ValueError:
            return 0
    return 0


def _session_flags(db, session_id: str) -> tuple[bool, bool]:
    if not session_id:
        return True, False
    sess = db.table('import_sessions').select('pull_stats, translate').eq('id', session_id).execute()
    row = sess.data[0] if sess.data else {}
    return row.get('pull_stats', True), row.get('translate', False)


async def _resolve_followers_safe(handle: str) -> int | None:
    """Обёртка над resolve_followers: обогащение метрик НЕ должно ронять джоб с уже скачанным
    (платным) аудио. get_followers_via_apify сам глотает свои ошибки, но
    get_followers_via_instaloader ловит только InstaloaderException — неожиданный JSON профиля
    (KeyError/AttributeError) или ленивый ImportError пролетели бы наверх и увели джоб в except
    Exception ниже, где finally удаляет WAV и джоб уходит на повторное платное скачивание.
    """
    from app.pipeline.followers import resolve_followers
    try:
        return await resolve_followers(handle)
    except Exception as exc:  # noqa: BLE001 — любой сбой обогащения не должен ронять джоб
        logger.warning('Подписчики @%s: не получены (%s) — продолжаю без них', handle, exc)
        return None


def _maybe_close_session(db, session_id: str) -> None:
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

    # Атомарный claim: берём джоб, только если он всё ещё queued. Проигравший воркер
    # получает пустой data и молча пропускает — двойной обработки не будет.
    # next_attempt_at здесь = момент claim'а: по нему ищем брошенные in_progress.
    claim = (
        db.table('jobs')
        .update({'state': 'in_progress', 'attempts': attempts, 'next_attempt_at': _utc_now()})
        .eq('id', job_id)
        .eq('state', 'queued')
        .execute()
    )
    if not claim.data:
        return

    # Дедуп: рилс уже расшифрован (другой сессией/джобом) — не тратим вызов API.
    existing = db.table('transcripts').select('status').eq('reel_id', reel_id).execute()
    if existing.data and existing.data[0].get('status') == 'done':
        db.table('jobs').update({'state': 'done', 'error': None}).eq('id', job_id).execute()
        _maybe_close_session(db, session_id)
        return

    db.table('transcripts').update({'status': 'downloading'}).eq('reel_id', reel_id).execute()

    audio_path = None
    try:
        reel_resp = db.table('reels').select('url').eq('id', reel_id).execute()
        url: str = reel_resp.data[0]['url']

        transcript_resp = (
            db.table('transcripts').select('engine, model').eq('reel_id', reel_id).execute()
        )
        model_name: str = (transcript_resp.data[0] if transcript_resp.data else {}).get('model') or 'medium'

        pull_stats, do_translate = _session_flags(db, session_id)

        audio_path, info = await download_audio(url, AUDIO_DIR)

        if pull_stats:
            meta = extract_metadata(info)
            # Подписчиков дотягиваем ЗДЕСЬ, единой точкой для всех источников (Apify/StarAPI/yt-dlp):
            # через resolve_followers (Apify profile-актор → instaloader-сессия), кэш по автору.
            # Заполняем только если источник их не дал — StarAPI-путь может отдать followers сам.
            if meta.get('author_followers') is None and meta.get('author_handle'):
                followers = await _resolve_followers_safe(meta['author_handle'])
                if followers is not None:
                    meta['author_followers'] = followers
            update = {k: v for k, v in meta.items() if v is not None}
            if update:
                db.table('reels').update(update).eq('id', reel_id).execute()

        db.table('transcripts').update({'status': 'transcribing'}).eq('reel_id', reel_id).execute()
        async with _transcription_sem:
            text, language, duration_sec, engine_used = await asyncio.to_thread(
                transcribe_audio, audio_path, model_name,
            )

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
            'engine': engine_used,   # фактический движок, а не заявленный при импорте
            'status': 'done',
            'fail_reason': None,
        }).eq('reel_id', reel_id).execute()

        db.table('jobs').update({'state': 'done', 'error': None}).eq('id', job_id).execute()
        logger.info('✓ %s (lang=%s, translate=%s)', url, language, do_translate)

    except NoAudioError as exc:
        # Фото/карусель — аудио нет, расшифровывать нечего. Терминально, без ретраев.
        # НО метрики (если источник их дал в exc.info) сохраняем: статистика важнее аудио.
        logger.info('📷 нет аудио, джоб %s: %s', job_id, exc)
        info = getattr(exc, 'info', None)
        if pull_stats and info:
            meta = extract_metadata(info)
            if meta.get('author_followers') is None and meta.get('author_handle'):
                followers = await _resolve_followers_safe(meta['author_handle'])
                if followers is not None:
                    meta['author_followers'] = followers
            update = {k: v for k, v in meta.items() if v is not None}
            if update:
                db.table('reels').update(update).eq('id', reel_id).execute()
        db.table('jobs').update({
            'state': 'failed', 'attempts': attempts, 'error': str(exc),
        }).eq('id', job_id).execute()
        try:
            db.table('transcripts').update({
                'status': 'no_audio', 'fail_reason': 'фото/карусель — нет аудио',
            }).eq('reel_id', reel_id).execute()
        except Exception:  # noqa: BLE001 — БД пока без 'no_audio' в CHECK: не роняем джоб
            db.table('transcripts').update({
                'status': 'failed', 'fail_reason': 'фото/карусель — нет аудио',
            }).eq('reel_id', reel_id).execute()

    except QuotaExceededError as exc:
        # Все ключи на cooldown — не тратим попытку, откладываем джоб на окно cooldown.
        # Счётчик отложений живёт в error ('quota_deferred:N'): после MAX_QUOTA_DEFERRALS
        # переводим в failed, чтобы джоб не крутился в очереди вечно.
        deferrals = _quota_deferrals(job.get('error')) + 1
        if deferrals > MAX_QUOTA_DEFERRALS:
            logger.warning('✗ квота не восстановилась за %d отложений, джоб %s → failed', deferrals - 1, job_id)
            db.table('jobs').update({
                'state': 'failed', 'attempts': attempts, 'error': str(exc),
            }).eq('id', job_id).execute()
            db.table('transcripts').update({
                'status': 'failed', 'fail_reason': 'исчерпана квота API-ключей',
            }).eq('reel_id', reel_id).execute()
        else:
            logger.warning('⏸ квота/cooldown, джоб %s отложен (%d/%d): %s',
                           job_id, deferrals, MAX_QUOTA_DEFERRALS, exc)
            next_at = (_utc_now_dt() + timedelta(minutes=settings.starapi_key_cooldown_min)).isoformat()
            db.table('jobs').update({
                'state': 'queued',
                'attempts': max(0, attempts - 1),
                'next_attempt_at': next_at,
                'error': f'quota_deferred:{deferrals}',
            }).eq('id', job_id).execute()
            db.table('transcripts').update({'status': 'queued'}).eq('reel_id', reel_id).execute()

    except Exception as exc:  # noqa: BLE001
        logger.error('✗ job %s: %s', job_id, exc)
        next_at = (_utc_now_dt() + timedelta(seconds=30 * attempts)).isoformat()
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
        try:
            if audio_path is not None:
                audio_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        _maybe_close_session(db, session_id)


def _requeue_stale(db) -> None:
    # Возвращаем в очередь только ДАВНО брошенные in_progress (воркер умер посреди джоба).
    # Свежие in_progress не трогаем — их прямо сейчас обрабатывает другой воркер.
    # next_attempt_at у in_progress = момент claim'а (см. _process_job).
    cutoff = (_utc_now_dt() - timedelta(minutes=STALE_JOB_MINUTES)).isoformat()
    stuck = (
        db.table('jobs')
        .update({'state': 'queued', 'next_attempt_at': _utc_now()})
        .eq('state', 'in_progress')
        .lt('next_attempt_at', cutoff)
        .execute()
    )
    if stuck.data:
        logger.info('Сброшено %d брошенных in_progress → queued', len(stuck.data))
        for j in stuck.data:
            db.table('transcripts').update({'status': 'queued'}).eq('reel_id', j['reel_id']).execute()


async def run_worker() -> None:
    global _transcription_sem
    _transcription_sem = asyncio.Semaphore(1)
    from app.pipeline.transcribe import active_asr_mode
    logger.info('Воркер запущен (AUDIO_DIR=%s, ASR=%s)', AUDIO_DIR, active_asr_mode())
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    import time
    last_stale_check = 0.0
    tick = 0

    while True:
        # Сетевой сбой в одной итерации не должен убивать воркер: рестарт процесса
        # оставляет asyncio-примитивы привязанными к мёртвому loop (Python 3.9),
        # и все последующие джобы падают с «attached to a different loop».
        try:
            db = get_db()
            if time.monotonic() - last_stale_check >= STALE_CHECK_INTERVAL_SEC or last_stale_check == 0.0:
                _requeue_stale(db)
                last_stale_check = time.monotonic()

            tick += 1
            # desc = свежий импорт первым; каждый 5-й проход — asc, чтобы старые
            # отложенные джобы не голодали вечно при постоянном потоке новых.
            newest_first = tick % 5 != 0
            now = _utc_now()
            jobs_resp = (
                db.table('jobs')
                .select('*')
                .eq('state', 'queued')
                .lte('next_attempt_at', now)
                .order('next_attempt_at', desc=newest_first)
                .limit(10)
                .execute()
            )
            jobs = jobs_resp.data
            if jobs:
                await asyncio.gather(*[_process_job(j) for j in jobs])
            else:
                await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Итерация цикла упала (%s) — продолжаю через 5с…', exc)
            await asyncio.sleep(5)


if __name__ == '__main__':
    import time
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    while True:
        try:
            asyncio.run(run_worker())
        except Exception as exc:
            logger.error('Воркер упал: %s — рестарт через 10с…', exc)
            time.sleep(10)
