"""Публичный просмотр сохранённого недельного набора без вызовов Meta."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Literal, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix='/radar/carousels', tags=['carousels'])
REPORT_URL = 'https://raw.githubusercontent.com/ilfaaat-sistema/reelscribe/refs/heads/data/authors-carousels-20261013-20261019/activity-report.json'
_cache = None
_cached_at = 0.0
_lock = asyncio.Lock()
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


async def _report():
    global _cache, _cached_at
    now = time.monotonic()
    if _cache is not None and now - _cached_at < 60:
        return _cache
    async with _lock:
        if _cache is not None and time.monotonic() - _cached_at < 60:
            return _cache
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                async with client.stream('GET', REPORT_URL, params={'v': int(time.time() // 60)}) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 25_000_000:
                            raise ValueError('Отчёт слишком большой')
            import json
            value = json.loads(content)
            if (value.get('provider') != 'meta_graph' or value.get('start_date') != '2026-10-13'
                    or value.get('end_date') != '2026-10-19' or not isinstance(value.get('items'), list)
                    or not isinstance(value.get('collection'), dict)):
                raise ValueError('Неверный недельный набор')
            _cache, _cached_at = value, time.monotonic()
            return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, 'Недельный сбор пока недоступен. Попробуйте обновить страницу позже.') from None


def page(report, author='', sort='likes', order='desc', limit=50, offset=0):
    items, seen = [], set()
    for raw in report.get('items', []):
        if not isinstance(raw, dict) or raw.get('media_type') != 'CAROUSEL_ALBUM' or raw.get('id') is None:
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


@router.get('')
async def list_carousels(author: str = Query('', pattern=r'^@?[A-Za-z0-9._]{0,30}$'),
                         sort: Literal['likes', 'comments', 'timestamp'] = 'likes',
                         order: Literal['desc', 'asc'] = 'desc',
                         limit: int = Query(50, ge=50, le=100), offset: int = Query(0, ge=0)):
    if limit not in (50, 100):
        raise HTTPException(422, 'Размер страницы: 50 или 100')
    return page(await _report(), author, sort, order, limit, offset)


@router.get('/{media_id}')
async def detail(media_id: str):
    if not re.fullmatch(r'[0-9]{1,40}', media_id):
        raise HTTPException(422, 'Некорректный ID карусели')
    report = await _report()
    for raw in report['items']:
        if str(raw.get('id')) == media_id and raw.get('media_type') == 'CAROUSEL_ALBUM':
            return _item(raw)
    raise HTTPException(404, 'Карусель не найдена в недельном сборе')
