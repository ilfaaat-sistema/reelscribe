"""Общий отбор каруселей: сохранённые отчёты и узкая RPC текущего anon-клиента."""
from __future__ import annotations

import asyncio
import json
import math
import re

from fastapi import HTTPException

from app.api import carousels
from app.core.db import get_db

COLUMNS = 'instagram_media_id,selected_at,snapshot,source_period'


def validate_id(media_id):
    if not isinstance(media_id, str) or not re.fullmatch(r'[0-9]{1,40}', media_id):
        raise HTTPException(422, 'Некорректный ID карусели')
    return media_id


def snapshot(raw, media_id):
    if not isinstance(raw, dict) or str(raw.get('id')) != media_id or raw.get('media_type') != 'CAROUSEL_ALBUM':
        raise HTTPException(422, 'Некорректный снимок карусели')
    result = carousels._item(raw)
    for key, value in result.items():
        if value is None:
            continue
        if key in ('likes', 'comments', 'views', 'followers'):
            if value > 9007199254740991 or not math.isfinite(value):
                result[key] = None
            continue
        if (not isinstance(value, str) or (key == 'username' and len(value) > 30)
                or (key == 'shortcode' and not re.fullmatch(r'[A-Za-z0-9_-]+', value))):
            raise HTTPException(422, 'Некорректный снимок карусели')
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 65536:
        raise HTTPException(422, 'Снимок карусели слишком большой')
    return result


def _item(row):
    media_id = validate_id(row.get('id', row.get('instagram_media_id')))
    return {'id': media_id, 'selected_at': row['selected_at'],
            'snapshot': snapshot(row['snapshot'], media_id), 'source_period': row['source_period']}


def _read_all():
    result = []
    start = 0
    while True:
        rows = (get_db().table('carousel_likes').select(COLUMNS).eq('active', True)
                .order('selected_at', desc=True).order('instagram_media_id')
                .range(start, start + 999).execute().data or [])
        result.extend(_item(row) for row in rows)
        if len(rows) < 1000:
            return {'items': result, 'total': len(result)}
        start += 1000


def _read_saved(media_id):
    rows = (get_db().table('carousel_likes').select(COLUMNS)
            .eq('instagram_media_id', media_id).limit(1).execute().data or [])
    return _item(rows[0]) if rows else None


def _write(media_id, selected, value=None, period=None):
    data = get_db().rpc('set_carousel_like', {'p_id': media_id, 'p_selected': selected,
                       'p_snapshot': value, 'p_period': '14d' if period == '30d' else period}).execute().data
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError('Неверный ответ записи')
    if selected:
        return {'item': _item(data)}
    if data.get('id') != media_id or data.get('selected') is not False:
        raise ValueError('Неверный ответ снятия отметки')
    return {'id': media_id, 'selected': False}


async def _database(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        raise HTTPException(503, 'Не удалось прочитать или сохранить отбор. Попробуйте позже.') from None


async def list_likes():
    return await _database(_read_all)


async def select(media_id, period):
    validate_id(media_id)
    failure = None
    for candidate in dict.fromkeys((period, '7d', '14d', 'archive14d', '30d')):
        try:
            value = snapshot(await carousels.detail(media_id, candidate), media_id)
        except HTTPException as error:
            if error.status_code not in (404, 503):
                raise
            if failure is None or error.status_code == 503:
                failure = error
            continue
        return await _database(_write, media_id, True, value, candidate)
    saved = await _database(_read_saved, media_id)
    if saved:
        return await _database(_write, media_id, True, saved['snapshot'], saved['source_period'])
    raise failure


async def deselect(media_id):
    validate_id(media_id)
    return await _database(_write, media_id, False)
