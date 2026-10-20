"""Личная подборка из сохранённых отчётов; запросы к БД идут с JWT владельца."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from app.api.carousels import FORTNIGHT_REPORT_URL, REPORT_URL, _item
from app.core.config import settings
from app.core.user_auth import get_current_token

ID_RE = re.compile(r'^[0-9]{1,40}$')
SORTS = {'selected_at', 'timestamp', 'likes', 'comments'}


class CarouselSelectionError(Exception):
    def __init__(self, status_code: int, detail: str | dict):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def _headers() -> dict[str, str]:
    token = get_current_token()
    return {'apikey': settings.supabase_anon_key, 'Authorization': f'Bearer {token}',
            'Accept': 'application/json', 'Content-Type': 'application/json'}


def _db(method: str, path: str, *, params: dict | None = None,
        payload: dict | None = None, headers: dict | None = None) -> tuple[Any, httpx.Headers]:
    url = settings.supabase_url.rstrip('/') + '/rest/v1/' + path
    request_headers = _headers()
    request_headers.update(headers or {})
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.request(method, url, params=params, json=payload, headers=request_headers)
    except httpx.HTTPError:
        raise CarouselSelectionError(503, 'Хранилище подборки временно недоступно') from None
    if response.status_code >= 400:
        try:
            error = response.json()
        except ValueError:
            error = {}
        code = error.get('code') if isinstance(error, dict) else None
        if code == '40001':
            raise CarouselSelectionError(409, 'Подборка изменилась на другом устройстве')
        if response.status_code in (401, 403):
            raise CarouselSelectionError(401, 'Войдите в аккаунт повторно')
        if response.status_code == 400 or code in ('22023', '23514'):
            raise CarouselSelectionError(422, 'Некорректные данные карусели')
        raise CarouselSelectionError(503, 'Не удалось прочитать или изменить подборку')
    if not response.content:
        return None, response.headers
    try:
        return response.json(), response.headers
    except ValueError:
        raise CarouselSelectionError(503, 'Некорректный ответ хранилища') from None


def _all_rows(user_id: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch, _ = _db('GET', 'carousel_selections', params={
            'select': 'instagram_media_id,selected_at,revision,snapshot,source_period,source_start_date,source_end_date,snapshot_at',
            'user_id': f'eq.{user_id}', 'removed_at': 'is.null',
            'order': 'selected_at.desc,instagram_media_id.asc', 'limit': 1000, 'offset': offset,
        })
        if not isinstance(batch, list):
            raise CarouselSelectionError(503, 'Некорректный ответ хранилища')
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        offset += 1000


def _row(user_id: str, media_id: str, active_only: bool = True) -> dict | None:
    params = {'select': 'instagram_media_id,selected_at,revision,snapshot,source_period,source_start_date,source_end_date,snapshot_at,removed_at',
              'user_id': f'eq.{user_id}', 'instagram_media_id': f'eq.{media_id}', 'limit': 1}
    if active_only:
        params['removed_at'] = 'is.null'
    value, _ = _db('GET', 'carousel_selections', params=params)
    if not isinstance(value, list):
        raise CarouselSelectionError(503, 'Некорректный ответ хранилища')
    return value[0] if value else None


def _public(row: dict) -> dict:
    snapshot = row.get('snapshot') if isinstance(row.get('snapshot'), dict) else {}
    safe = _item(snapshot)
    return {'id': row['instagram_media_id'], 'selected': row.get('removed_at') is None,
            'selected_at': row['selected_at'], 'revision': row['revision'],
            'snapshot': safe, 'source_period': row['source_period'],
            'source_start_date': row['source_start_date'], 'source_end_date': row['source_end_date'],
            'snapshot_at': row['snapshot_at']}


def _sortable(row: dict, sort: str):
    value = row['selected_at'] if sort == 'selected_at' else row['snapshot'].get(sort)
    if value is None:
        return None
    if sort in ('timestamp', 'selected_at'):
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def list_selections(user_id: str, q: str, sort: str, order: str, limit: int, offset: int) -> dict:
    if sort not in SORTS or order not in ('asc', 'desc') or limit not in (50, 100) or offset < 0:
        raise CarouselSelectionError(422, 'Некорректные параметры списка')
    rows = _all_rows(user_id)
    selected_total = len(rows)
    needle = q.strip().lstrip('@').casefold()
    if needle:
        rows = [row for row in rows if needle in str(row['snapshot'].get('username') or '').casefold()
                or needle in str(row['snapshot'].get('caption') or '').casefold()]
    # Tie-breaker не зависит от направления сортировки.
    rows.sort(key=lambda row: row['instagram_media_id'])
    present = [row for row in rows if _sortable(row, sort) is not None]
    missing = [row for row in rows if _sortable(row, sort) is None]
    present.sort(key=lambda row: _sortable(row, sort), reverse=order == 'desc')
    return {'items': [_public(row) for row in (present + missing)[offset:offset + limit]],
            'total': len(rows), 'selected_total': selected_total}


def selection_status(user_id: str, ids: list[str]) -> dict:
    if len(ids) > 100 or any(not ID_RE.fullmatch(media_id) for media_id in ids):
        raise CarouselSelectionError(422, 'Некорректные ID каруселей')
    selected_total = len(_all_rows(user_id))
    if not ids:
        return {'states': [], 'selected_total': selected_total}
    rows, _ = _db('GET', 'carousel_selections', params={
        'select': 'instagram_media_id,revision,removed_at',
        'user_id': f'eq.{user_id}',
        'instagram_media_id': 'in.(' + ','.join(dict.fromkeys(ids)) + ')',
    })
    if not isinstance(rows, list):
        raise CarouselSelectionError(503, 'Некорректный ответ хранилища')
    by_id = {row['instagram_media_id']: row for row in rows}
    return {'states': [{'id': media_id, 'selected': media_id in by_id and by_id[media_id]['removed_at'] is None,
                        'revision': by_id[media_id]['revision'] if media_id in by_id else 0}
                       for media_id in dict.fromkeys(ids)], 'selected_total': selected_total}


def get_selection(user_id: str, media_id: str) -> dict:
    if not ID_RE.fullmatch(media_id):
        raise CarouselSelectionError(422, 'Некорректный ID карусели')
    row = _row(user_id, media_id)
    if row is None:
        raise CarouselSelectionError(404, 'Карусель не найдена в вашей подборке')
    return _public(row)


def _report_item(media_id: str, period: str) -> tuple[dict, dict] | None:
    url = FORTNIGHT_REPORT_URL if period == '14d' else REPORT_URL
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get(url)
        if response.status_code >= 400 or len(response.content) > 25_000_000:
            raise CarouselSelectionError(503, 'Сохранённый отчёт временно недоступен')
        report = response.json()
    except (httpx.HTTPError, ValueError):
        raise CarouselSelectionError(503, 'Сохранённый отчёт временно недоступен') from None
    if (not isinstance(report, dict) or report.get('provider') != 'meta_graph'
            or report.get('start_date') != ('2026-10-06' if period == '14d' else '2026-10-13')
            or report.get('end_date') != '2026-10-19' or not isinstance(report.get('items'), list)):
        raise CarouselSelectionError(503, 'Сохранённый отчёт временно недоступен')
    for raw in report['items']:
        if isinstance(raw, dict) and str(raw.get('id')) == media_id and raw.get('media_type') == 'CAROUSEL_ALBUM':
            return _item(raw), report
    return None


def select_carousel(user_id: str, media_id: str, period: str, expected_revision: int) -> dict:
    if not ID_RE.fullmatch(media_id) or period not in ('7d', '14d') or expected_revision < 0:
        raise CarouselSelectionError(422, 'Некорректные данные отбора')
    existing = _row(user_id, media_id, active_only=False)
    if existing and existing['removed_at'] is None:
        return {'item': _public(existing), 'selected_total': len(_all_rows(user_id))}
    try:
        found = _report_item(media_id, period)
    except CarouselSelectionError:
        if not existing:
            raise
        found = None
    if found:
        snapshot, report = found
        start_date, end_date = report['start_date'], report['end_date']
    elif existing:
        snapshot = _item(existing['snapshot'])
        start_date, end_date = existing['source_start_date'], existing['source_end_date']
        period = existing['source_period']
    else:
        raise CarouselSelectionError(404, 'Карусель не найдена в сохранённом отчёте')
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 65536:
        raise CarouselSelectionError(422, 'Текст карусели превышает допустимый размер')
    result, _ = _db('POST', 'rpc/select_carousel', payload={
        'p_id': media_id, 'p_snapshot': snapshot, 'p_period': period,
        'p_start_date': start_date, 'p_end_date': end_date,
        'p_expected_revision': expected_revision,
    })
    if not isinstance(result, dict) or not isinstance(result.get('item'), dict):
        raise CarouselSelectionError(503, 'Некорректный ответ хранилища')
    result['item']['snapshot'] = _item(result['item']['snapshot'])
    return result


def remove_selection(user_id: str, media_id: str, expected_revision: int) -> dict:
    if not ID_RE.fullmatch(media_id) or expected_revision < 0:
        raise CarouselSelectionError(422, 'Некорректные данные отбора')
    result, _ = _db('POST', 'rpc/remove_carousel', payload={
        'p_id': media_id, 'p_expected_revision': expected_revision,
    })
    if not isinstance(result, dict) or result.get('id') != media_id:
        raise CarouselSelectionError(503, 'Некорректный ответ хранилища')
    return result
