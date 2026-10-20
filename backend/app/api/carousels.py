"""Публичный просмотр сохранённых наборов без вызовов Meta."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta
from typing import Literal, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api import carousel_favorites
from app.core.db import get_db

router = APIRouter(prefix='/radar/carousels', tags=['carousels'])
REPORT_URL = 'https://raw.githubusercontent.com/ilfaaat-sistema/reelscribe/refs/heads/data/authors-carousels-20261013-20261019/activity-report.json'
FORTNIGHT_REPORT_URL = REPORT_URL.replace('20261013', '20261006')
_cache = {}
_cached_at = {}
_lock = asyncio.Lock()
_source_authors_cache = None
_source_cached_at = 0.0
_source_lock = asyncio.Lock()
ITEM_KEYS = ('id', 'username', 'permalink', 'shortcode', 'timestamp', 'caption', 'media_type',
             'media_url', 'thumbnail_url', 'likes', 'comments', 'views', 'followers', 'collected_at')
SUMMARY_KEYS = ('start_date', 'end_date', 'timezone', 'collected_at', 'collection', 'totals', 'profiles')


def _safe_url(value, media=False):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        allowed = ('instagram.com', 'cdninstagram.com', 'fbcdn.net') if media else ('instagram.com',)
        if url.scheme == 'https' and not url.username and not url.password and any(host == h or host.endswith('.' + h) for h in allowed):
            return value
    except ValueError:
        pass
    return None


def _item(item):
    result = {key: item.get(key) for key in ITEM_KEYS}
    result['id'] = str(result['id'])
    result['permalink'] = _safe_url(result['permalink'])
    for field in ('media_url', 'thumbnail_url'):
        result[field] = _safe_url(result[field], media=True)
    for field in ('likes', 'comments', 'views', 'followers'):
        value = result[field]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            result[field] = None
    return result


async def _report(period='7d'):
    global _cache, _cached_at
    now = time.monotonic()
    if period in _cache and now - _cached_at[period] < 60:
        return _cache[period]
    async with _lock:
        if period in _cache and time.monotonic() - _cached_at[period] < 60:
            return _cache[period]
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                async with client.stream('GET', FORTNIGHT_REPORT_URL if period == '14d' else REPORT_URL, params={'v': int(time.time() // 60)}) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 25_000_000:
                            raise ValueError('Отчёт слишком большой')
            import json
            value = json.loads(content)
            if (value.get('provider') != 'meta_graph' or value.get('start_date') != ('2026-10-06' if period == '14d' else '2026-10-13')
                    or value.get('end_date') != '2026-10-19' or not isinstance(value.get('items'), list)
                    or not isinstance(value.get('collection'), dict)):
                raise ValueError('Неверный набор каруселей')
            if period == '14d' and value['collection'].get('target') != 586:
                raise ValueError('Неверный размер целевой очереди')
            _cache[period], _cached_at[period] = value, time.monotonic()
            return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, 'Сбор за 14 дней ещё не опубликован или временно недоступен. Выберите 7 дней или попробуйте позже.' if period == '14d' else 'Недельный сбор пока недоступен. Попробуйте обновить страницу позже.') from None


async def _selected_report(period=None):
    if period == 'archive14d':
        return await _report('14d')
    if period:
        return await _report(period)
    try:
        return await _report('14d')
    except HTTPException as error:
        if error.status_code != 503:
            raise
        return await _report('7d')


def _read_neyro_source_authors():
    """Source tags already exposed through /api/reels; read only author handles."""
    names = set()
    batch = 500
    for start in range(0, 5000, batch):
        rows = (get_db().table('reels')
                .select('author_handle,reel_sources!inner(account)')
                .eq('reel_sources.account', 'neyro_set7')
                .order('id').range(start, start + batch - 1).execute().data or [])
        names.update(name for row in rows
                     if isinstance(row, dict)
                     for name in [str(row.get('author_handle') or '').lstrip('@').lower()]
                     if re.fullmatch(r'[a-z0-9._]{1,30}', name))
        if len(rows) < batch:
            return names
    raise ValueError('Слишком много записей источника')


async def _neyro_source_authors():
    global _source_authors_cache, _source_cached_at
    if _source_authors_cache is not None and time.monotonic() - _source_cached_at < 300:
        return _source_authors_cache
    async with _source_lock:
        if _source_authors_cache is not None and time.monotonic() - _source_cached_at < 300:
            return _source_authors_cache
        try:
            result = await asyncio.to_thread(_read_neyro_source_authors)
        except Exception:
            raise HTTPException(503, 'Не удалось прочитать авторов NEYRO_SET7. Попробуйте позже.') from None
        _source_authors_cache, _source_cached_at = result, time.monotonic()
        return result


def page(report, author='', sort='likes', order='desc', limit=50, offset=0,
         exclude_authors=(), include_authors=None):
    items, seen = [], set()
    excluded = {username.lower().lstrip('@') for username in exclude_authors}
    included = ({username.lower().lstrip('@') for username in include_authors}
                if include_authors is not None else None)
    for raw in report.get('items', []):
        if not isinstance(raw, dict) or raw.get('media_type') != 'CAROUSEL_ALBUM' or raw.get('id') is None:
            continue
        if raw.get('username', '').lower() in excluded:
            continue
        if included is not None and raw.get('username', '').lower() not in included:
            continue
        if author and raw.get('username', '').lower() != author.lower().lstrip('@'):
            continue
        identity = str(raw['id'])
        if identity in seen:
            continue
        seen.add(identity)
        items.append(_item(raw))
    # ID задаёт стабильный порядок совпадающих значений; неизвестные всегда в конце.
    items.sort(key=lambda row: row['id'])
    present = [row for row in items if row.get(sort) is not None]
    missing = [row for row in items if row.get(sort) is None]
    present.sort(key=lambda row: row[sort], reverse=order == 'desc')
    result = {key: report.get(key) for key in SUMMARY_KEYS}
    result.update(items=(present + missing)[offset:offset + limit], total=len(items))
    return result


async def _rolling_page(*, favorites_only=True, **filters):
    archives = []
    if not favorites_only:
        reports = await asyncio.gather(_report('7d'), _report('14d'))
        archives = [item for report in reports for item in report['items']]
    return await asyncio.to_thread(carousel_favorites.live_page, favorites_only=favorites_only,
                                   archive_items=archives, **filters)


async def _rolling_detail(media_id):
    try:
        return await asyncio.to_thread(carousel_favorites.live_detail, media_id)
    except HTTPException as error:
        if error.status_code != 404:
            raise
    reports = await asyncio.gather(_report('7d'), _report('14d'))
    start, end = carousel_favorites.window()
    for report in reports:
        for row in carousel_favorites._state_items(report, start, end, None):
            if str(row.get('id')) == media_id:
                return {**_item(row), 'start_date': start.date().isoformat(),
                        'end_date': (end - timedelta(days=1)).date().isoformat()}
    raise HTTPException(404, 'Карусель не найдена в текущем окне')


@router.get('')
async def list_carousels(author: str = Query('', pattern=r'^@?[A-Za-z0-9._]{0,30}$'),
                         sort: Literal['likes', 'comments', 'timestamp'] = 'likes',
                         order: Literal['desc', 'asc'] = 'desc',
                         limit: int = Query(50, ge=50, le=100), offset: int = Query(0, ge=0),
                         period: Optional[Literal['', '7d', '14d', 'archive14d']] = None,
                         favorites_only: bool = True):
    if limit not in (50, 100):
        raise HTTPException(422, 'Размер страницы: 50 или 100')
    if period == '14d':
        return await _rolling_page(favorites_only=favorites_only, author=author, sort=sort,
                                   order=order, limit=limit, offset=offset)
    return page(await _selected_report(period), author, sort, order, limit, offset)


class CarouselQuery(BaseModel):
    favorites_only: bool = True
    period: Optional[Literal['', '7d', '14d', 'archive14d']] = None
    author: str = Field('', pattern=r'^@?[A-Za-z0-9._]{0,30}$')
    sort: Literal['likes', 'comments', 'timestamp'] = 'likes'
    order: Literal['desc', 'asc'] = 'desc'
    limit: Literal[50, 100] = 50
    offset: int = Field(0, ge=0)
    exclude_authors: list[str] = Field(default_factory=list, max_length=1600)
    include_authors: Optional[list[str]] = Field(None, max_length=1600)
    source_account: Optional[Literal['neyro_set7']] = None

    @field_validator('exclude_authors', 'include_authors')
    @classmethod
    def valid_authors(cls, names):
        if names is None:
            return None
        result = []
        for name in names:
            value = name.lower().lstrip('@')
            if not re.fullmatch(r'[a-z0-9._]{1,30}', value):
                raise ValueError('Некорректное имя автора')
            if value not in result:
                result.append(value)
        return result


@router.post('')
async def filtered_carousels(query: CarouselQuery):
    """Только чтение: исключения приходят из настроек просмотра, без записи в коллекцию."""
    filters = query.model_dump()
    source = filters.pop('source_account')
    period = filters.pop('period')
    favorites_only = filters.pop('favorites_only')
    if period == '14d':
        if source == 'neyro_set7':
            filters['source_account'] = await _neyro_source_authors()
        else:
            filters['source_account'] = None
        return await _rolling_page(favorites_only=favorites_only, **filters)
    if source == 'neyro_set7':
        allowed = await _neyro_source_authors()
        included = filters['include_authors']
        filters['include_authors'] = sorted(allowed if included is None else allowed.intersection(included))
    return page(await _selected_report(period), **filters)


@router.get('/{media_id}')
async def detail(media_id: str, period: Optional[Literal['', '7d', '14d', 'archive14d']] = None):
    if not re.fullmatch(r'[0-9]{1,40}', media_id):
        raise HTTPException(422, 'Некорректный ID карусели')
    if period == '14d':
        return await _rolling_detail(media_id)
    report = await _selected_report(period)
    for raw in report['items']:
        if str(raw.get('id')) == media_id and raw.get('media_type') == 'CAROUSEL_ALBUM':
            return {**_item(raw), 'start_date': report['start_date'], 'end_date': report['end_date']}
    # Старые прямые ссылки не содержат period. После появления 14-дневного набора
    # они должны продолжать открывать карточки, оставшиеся только в недельном.
    if not period and report.get('start_date') == '2026-10-06':
        weekly = await _report('7d')
        for raw in weekly['items']:
            if str(raw.get('id')) == media_id and raw.get('media_type') == 'CAROUSEL_ALBUM':
                return {**_item(raw), 'start_date': weekly['start_date'], 'end_date': weekly['end_date']}
    raise HTTPException(404, 'Карусель не найдена в выбранном сборе')
