from __future__ import annotations

import csv
import io
import json
import re
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.core.db import get_db
from app.models.schemas import ErrorGroup, ErrorItem, ErrorReport

router = APIRouter(tags=["errors"])

_RU = {
    'reason': 'Причина',
    'shortcode': 'Код',
    'url': 'Ссылка',
    'author_handle': 'Автор',
    'attempts': 'Попытки',
    'state': 'Статус',
    'error_text': 'Текст ошибки',
}

# PostgREST молча режет .select() без range/limit на ~1000 строк — забираем страницами.
_PAGE_SIZE = 1000
# Строка URL с .in_(id_col, ids) на несколько тысяч UUID превышает лимит длины URI (HTTP 414) —
# бьём на чанки и склеиваем результаты.
_CHUNK_SIZE = 200

# Классификатор сырого текста ошибки (job.error / transcript.fail_reason) в человекочитаемый
# класс — для группировки. Формулировки взяты из реальных raise/сообщений в пайплайне:
# download.py, instaloader_downloader.py, starapi_downloader.py, apify_client.py,
# apify_profile_downloader.py, apify_downloader.py, workers/run.py. Порядок проверки важен —
# от самого специфичного/надёжного сигнала к самому общему, иначе более общий паттерн
# (например "429" внутри "все ключи на cooldown после 429") перехватит чужой класс.
_ERROR_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    ('Нет аудио (фото/карусель)', re.compile(
        r'фото/карусель|нет аудио|пост без видео', re.I,
    )),
    ('Исчерпана квота API-ключей', re.compile(
        r'исчерпан[а-я]* квота|все (?:ключи|токены) на cooldown|исчерпали кредит'
        r'|hard limit|monthly usage|platform-feature-disabled|\b402\b', re.I,
    )),
    ('Лимит запросов (rate-limit)', re.compile(
        r'rate-limit|too many requests|http 429|\b429\b|please wait', re.I,
    )),
    ('Приватный аккаунт или нужен логин', re.compile(
        r'приватн|нужен логин|login[ _-]?required', re.I,
    )),
    ('Пост не найден или удалён', re.compile(
        r'нет в свежей выдаче|нет данных по|пост не найден|удал[её]н', re.I,
    )),
    ('Сеть/таймаут', re.compile(
        r'timeout|timed out|connection|\bсеть\b|network|http 5\d\d', re.I,
    )),
    ('Ошибка распознавания', re.compile(
        r'whisper|распознав|transcrib|\basr\b', re.I,
    )),
]


def _classify(reason: str) -> str:
    """Сырой текст ошибки → класс для группировки. Сырой текст при этом не теряем — он остаётся
    в ErrorItem.fail_reason/job_error и в экспорте, классификация нужна только для группировки."""
    for label, pattern in _ERROR_CLASSES:
        if pattern.search(reason):
            return label
    return 'Прочее'


def _chunks(seq: list[Any], size: int) -> Any:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fetch_all_jobs(db, session: Optional[UUID]) -> list[dict]:
    """Все failed-джобы постранично (.range) — без этого PostgREST молча обрезает ответ на ~1000."""
    rows: list[dict] = []
    offset = 0
    while True:
        q = (
            db.table('jobs')
            .select('reel_id, error, attempts, state, next_attempt_at')
            .eq('state', 'failed')
            .order('reel_id')   # детерминированный порядок — иначе страницы могут пересекаться/терять строки
        )
        if session:
            q = q.eq('session_id', str(session))
        page = q.range(offset, offset + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def _fetch_in_chunks(
    db, table: str, select: str, id_col: str, ids: list[str], **filters: Any,
) -> list[dict]:
    """.in_(id_col, ids) чанками по _CHUNK_SIZE, склеивая результаты — иначе тысячи UUID
    в URL превышают лимит длины URI PostgREST (HTTP 414)."""
    rows: list[dict] = []
    for chunk in _chunks(ids, _CHUNK_SIZE):
        q = db.table(table).select(select).in_(id_col, chunk)
        for key, value in filters.items():
            q = q.in_(key, value) if isinstance(value, list) else q.eq(key, value)
        rows.extend(q.execute().data)
    return rows


def _collect_items(session: Optional[UUID], include_no_audio: bool) -> list[ErrorItem]:
    """Собрать упавшие рилсы: jobs(state='failed') + transcripts + reels, без N+1.

    По умолчанию (include_no_audio=False) терминальные "нет аудио" (фото/карусель) не
    считаются реальной ошибкой (их и ретраить бессмысленно — см. services/jobs.py) и
    скрыты из отчёта; include_no_audio=True показывает и их.
    """
    db = get_db()

    jobs = _fetch_all_jobs(db, session)
    if not jobs:
        return []

    reel_ids = list({j['reel_id'] for j in jobs})

    transcripts = _fetch_in_chunks(
        db, 'transcripts', 'reel_id, status, fail_reason', 'reel_id', reel_ids,
        status=['failed', 'no_audio'],
    )
    t_by_reel = {t['reel_id']: t for t in transcripts}

    reels = _fetch_in_chunks(
        db, 'reels', 'id, shortcode, url, author_handle', 'id', reel_ids,
    )
    r_by_id = {r['id']: r for r in reels}

    items: list[ErrorItem] = []
    for j in jobs:
        rid = j['reel_id']
        r = r_by_id.get(rid)
        if not r:
            continue
        t = t_by_reel.get(rid)
        if t and t.get('status') == 'no_audio' and not include_no_audio:
            continue
        fail_reason = t.get('fail_reason') if t else None
        items.append(ErrorItem(
            reel_id=rid,
            shortcode=r['shortcode'],
            url=r['url'],
            author_handle=r.get('author_handle'),
            fail_reason=fail_reason,
            job_error=j.get('error'),
            attempts=j.get('attempts') or 0,
            state=j['state'],
            next_attempt_at=j.get('next_attempt_at'),
        ))
    return items


def _group(items: list[ErrorItem]) -> list[ErrorGroup]:
    """Группируем по КЛАССУ ошибки, не по сырому тексту: сырой текст (shortcode/URL внутри)
    делает каждую запись уникальной, и группировка вырождается в список из групп по одному
    элементу. Сырой текст никуда не пропадает — он остаётся в ErrorItem.fail_reason/job_error."""
    groups: dict[str, list[ErrorItem]] = {}
    for it in items:
        raw = it.fail_reason or it.job_error or 'неизвестно'
        label = _classify(raw) if raw != 'неизвестно' else 'Прочее'
        groups.setdefault(label, []).append(it)
    result = [ErrorGroup(reason=label, count=len(its), items=its) for label, its in groups.items()]
    result.sort(key=lambda g: g.count, reverse=True)
    return result


@router.get("/errors", response_model=ErrorReport)
async def get_errors(
    session: Optional[UUID] = Query(None),
    include_no_audio: bool = Query(False),
) -> ErrorReport:
    items = _collect_items(session, include_no_audio)
    return ErrorReport(total=len(items), groups=_group(items), session=session)


@router.get("/errors/export")
async def export_errors(
    format: Annotated[Literal["csv", "json"], Query()] = "csv",  # noqa: A002
    session: Optional[UUID] = Query(None),
    include_no_audio: bool = Query(False),
) -> Response:
    items = _collect_items(session, include_no_audio)
    groups = _group(items)

    rows = []
    for g in groups:
        for it in g.items:
            rows.append({
                'reason': g.reason,
                'shortcode': it.shortcode,
                'url': it.url,
                'author_handle': it.author_handle or '',
                'attempts': it.attempts,
                'state': it.state,
                'error_text': it.job_error or it.fail_reason or '',
            })

    if format == 'csv':
        return _as_csv(rows)
    if format == 'json':
        return _as_json(rows)
    raise HTTPException(501, f"формат {format} ещё не реализован")


def _as_csv(rows: list[dict]) -> StreamingResponse:
    fields = ['reason', 'shortcode', 'url', 'author_handle', 'attempts', 'state', 'error_text']
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writerow({f: _RU.get(f, f) for f in fields})
    for r in rows:
        writer.writerow(r)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="reelscribe_errors.csv"'},
    )


def _as_json(rows: list[dict]) -> Response:
    return Response(
        content=json.dumps(rows, ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': 'attachment; filename="reelscribe_errors.json"'},
    )
