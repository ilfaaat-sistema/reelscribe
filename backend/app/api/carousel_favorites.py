"""Общее избранное и живые карусели в отдельной ветке данных GitHub."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.radar.live_favorites_store import Conflict, LiveFavoritesStore
from app.radar.carousel_window import rolling_window

router = APIRouter(prefix='/radar/carousels/favorites', tags=['carousels'])
NAME = re.compile(r'[a-z0-9._]{1,30}\Z')
RESERVED = {'p', 'reel', 'reels', 'stories', 'explore', 'accounts', 'direct'}
MOSCOW = ZoneInfo('Europe/Moscow')
PUBLIC_FIELDS = ('username', 'status', 'last_checked_at', 'next_check_at', 'error', 'history_days', 'coverage_version')


def _store(client):
    if not settings.github_dispatch_token:
        raise HTTPException(503, 'Общее избранное пока не настроено на сервере')
    return LiveFavoritesStore(client, settings.github_dispatch_token)


def username_from_profile(value: str) -> str:
    value = value.strip()
    if value.startswith('@'):
        username = value[1:]
    elif value.startswith('https://'):
        url = urlsplit(value)
        if (url.hostname not in ('instagram.com', 'www.instagram.com') or url.username or url.password
                or url.port is not None or url.fragment or not url.path.startswith('/')):
            raise ValueError('Нужна ссылка на профиль Instagram')
        username = url.path.strip('/')
        if '/' in username:
            raise ValueError('Нужна ссылка на профиль, а не на публикацию')
    else:
        username = value
    username = username.lower()
    if not NAME.fullmatch(username) or username in RESERVED:
        raise ValueError('Некорректное имя профиля Instagram')
    return username


def _public(row):
    return {key: row.get(key) for key in PUBLIC_FIELDS}


def _load(store):
    try:
        return store.load()
    except Exception:  # noqa: BLE001 — ответ GitHub не должен раскрывать токен или внутренний URL.
        raise HTTPException(503, 'Не удалось загрузить общий список. Попробуйте позже.') from None


def _read_state(store):
    try:
        return store.read_state()
    except Exception:  # noqa: BLE001 — ответ GitHub не должен раскрывать токен или внутренний URL.
        raise HTTPException(503, 'Не удалось загрузить общий список. Попробуйте позже.') from None


@router.get('')
def list_favorites():
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        state = _read_state(_store(client))
    return {'authors': [_public(row) for row in state['authors'] if row.get('active', True)]}


class AddFavorite(BaseModel):
    profile: str = Field(min_length=1, max_length=300)


def _mutate(username, *, add):
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        store = _store(client)
        for _ in range(4):
            state, sha = _load(store)
            authors = state['authors']
            row = next((row for row in authors if row['username'] == username), None)
            if add:
                if row and row.get('active', True):
                    return _public(row)
                if sum(bool(item.get('active', True)) for item in authors) >= 200:
                    raise HTTPException(429, 'Достигнут защитный лимит избранных авторов')
                if row:
                    row.update(active=True, status='pending', error=None, checkpoint={}, next_check_at=None)
                else:
                    now = datetime.now(timezone.utc)
                    today = now.astimezone(MOSCOW).date()
                    if sum(datetime.fromisoformat(item['added_at'].replace('Z', '+00:00')).astimezone(MOSCOW).date() == today
                           for item in authors) >= 30:
                        raise HTTPException(429, 'Защитный дневной лимит новых авторов достигнут')
                    row = {'username': username, 'active': True, 'status': 'pending',
                           'added_at': now.isoformat(), 'last_checked_at': None,
                           'next_check_at': None, 'error': None, 'checkpoint': {}}
                    authors.append(row)
            else:
                if not row or not row.get('active', True):
                    return None
                row['active'] = False
            try:
                store.save(state, sha)
                return _public(row) if add else None
            except Conflict:
                continue
            except Exception:  # noqa: BLE001 — не раскрываем токен и ответ GitHub.
                raise HTTPException(503, 'Не удалось сохранить избранное. Попробуйте позже.') from None
    raise HTTPException(503, 'Список меняется одновременно. Повторите действие.')


@router.post('', status_code=201)
def add_favorite(body: AddFavorite):
    try:
        username = username_from_profile(body.profile)
    except (ValueError, TypeError):
        raise HTTPException(422, 'Вставьте ссылку на профиль Instagram или @имя') from None
    return _mutate(username, add=True)


@router.delete('/{username}', status_code=204)
def remove_favorite(username: str):
    try:
        username = username_from_profile(username)
    except (ValueError, TypeError):
        raise HTTPException(422, 'Некорректное имя автора') from None
    _mutate(username, add=False)
    return Response(status_code=204)


def window(history_days=14, now=None):
    return rolling_window(now or datetime.now(MOSCOW), history_days)


def _moment(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _state_items(state, start, end, names):
    first, last = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    return [row for row in state['items'] if isinstance(row, dict) and isinstance(row.get('username'), str)
            and (names is None or row.get('username') in names)
            and row.get('media_type') == 'CAROUSEL_ALBUM' and (stamp := _moment(row.get('timestamp')))
            and first <= stamp < last]


def _sorted_items(items, sort, order):
    items = sorted(items, key=lambda row: str(row.get('id')))
    present = [row for row in items if row.get(sort) is not None]
    missing = [row for row in items if row.get(sort) is None]
    present.sort(key=lambda row: _moment(row['timestamp']) if sort == 'timestamp' else row[sort],
                 reverse=order == 'desc')
    return present + missing


def live_page(*, author='', sort='likes', order='desc', limit=50, offset=0,
              exclude_authors=(), include_authors=None, source_account=None, favorites_only=True, archive_items=(), history_days=14):
    from app.api.carousels import _item

    start, end = window(history_days)
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        state = _read_state(_store(client))
    active = [row for row in state['authors'] if row.get('active', True)]
    names = {row['username'] for row in active}
    raw_items = list(archive_items) + state['items'] if not favorites_only else state['items']
    in_window = _state_items({'items': raw_items}, start, end, names if favorites_only else None)
    all_items = list({str(row['id']): row for row in in_window if row.get('id') is not None}.values())
    if not favorites_only:
        names = {row['username'] for row in all_items}
    profile_counts = {name: 0 for name in names}
    for row in all_items:
        profile_counts[row['username']] += 1
    selected = names - {name.lower().lstrip('@') for name in exclude_authors}
    if include_authors is not None:
        selected &= {name.lower().lstrip('@') for name in include_authors}
    if author:
        selected &= {author.lower().lstrip('@')}
    if source_account is not None:
        selected &= set(source_account)
    items = _sorted_items([row for row in all_items if row['username'] in selected], sort, order)
    def coverage_status(row):
        if history_days == 30 and row['status'] == 'updated' and (row.get('history_days', 0) < 30 or row.get('coverage_version') != 2):
            return 'pending'
        return row['status']

    statuses = [coverage_status(row) for row in active]
    status = ('cooldown' if 'meta_paused' in statuses else
              'running' if any(value in ('pending', 'collecting') for value in statuses) else
              'failed' if 'error' in statuses else 'finished')
    checked = sum(value in ('updated', 'unavailable') for value in statuses)
    last_checked = [row['last_checked_at'] for row in active if row.get('last_checked_at')]
    collected = [row['collected_at'] for row in all_items if _moment(row.get('collected_at'))]
    latest = max(last_checked + collected, key=_moment, default=None)
    return {
        'start_date': start.date().isoformat(),
        'end_date': (end - timedelta(days=1)).date().isoformat(),
        'timezone': 'Europe/Moscow',
        'collected_at': latest,
        'collection': {'status': status, 'target': len(active), 'checked': checked,
                       'available': sum(value == 'updated' for value in statuses),
                       'unavailable': sum(value == 'unavailable' for value in statuses),
                       'pending': len(active) - checked,
                       'cooldown_until': min((row['next_check_at'] for row in active
                                              if row['status'] == 'meta_paused' and row.get('next_check_at')),
                                             default=None),
                       'review_required': sum(value == 'error' for value in statuses)},
        'totals': {'carousels': len(items)},
        'profiles': ([{'username': row['username'], 'carousels': profile_counts[row['username']]
                       if coverage_status(row) in ('updated', 'unavailable') else None} for row in active]
                     if favorites_only else [{'username': name, 'carousels': profile_counts[name]} for name in sorted(names)]),
        'items': [_item(row) for row in items[offset:offset + limit]],
        'total': len(items),
    }


def live_detail(media_id: str, history_days=14):
    from app.api.carousels import _item

    start, end = window(history_days)
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        state = _read_state(_store(client))
    for row in _state_items(state, start, end, None):
        if str(row.get('id')) == media_id:
            return {**_item(row), 'start_date': start.date().isoformat(),
                    'end_date': (end - timedelta(days=1)).date().isoformat()}
    raise HTTPException(404, 'Карусель не найдена')
