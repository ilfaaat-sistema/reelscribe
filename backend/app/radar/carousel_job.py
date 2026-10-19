"""Возобновляемый сбор недельных каруселей через Instagram Business Discovery."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.radar.activity_job import (
    LIMIT_CODES,
    _now,
    _retry_after,
    _selection,
    _stamp,
    _usage,
    atomic_json,
)

SCAN_FIELDS = 'id,media_type,timestamp'
DETAIL_FIELDS = ('id,media_type,timestamp,permalink,caption,media_url,thumbnail_url,'
                 'like_count,comments_count')
SHORTCODE = re.compile(r'[A-Za-z0-9_-]+\Z')
PUBLIC_SECRET_PATTERNS = re.compile(r'access_token=[^\s&]*|Bearer\s+[^\s]*|gh[opsu]_[A-Za-z0-9]{30,}', re.I)


def _caption(value):
    if not isinstance(value, str):
        return None, 0
    return PUBLIC_SECRET_PATTERNS.subn('[маркер скрыт]', value)


def _media_url(value):
    """В отчёт попадают лишь ссылки Instagram CDN с безопасной схемой."""
    if not isinstance(value, str):
        return None
    if PUBLIC_SECRET_PATTERNS.search(value):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
        return None
    if host in ('instagram.com', 'cdninstagram.com', 'fbcdn.net') or host.endswith(
            ('.instagram.com', '.cdninstagram.com', '.fbcdn.net')):
        return value
    return None


def _permalink(value):
    if not isinstance(value, str):
        return None, None
    parsed = urlparse(value)
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in ('instagram.com', 'www.instagram.com'):
        return None, None
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 2 or parts[0] not in ('p', 'reel', 'tv') or not SHORTCODE.fullmatch(parts[1]):
        return None, None
    return f'https://www.instagram.com/{parts[0]}/{parts[1]}/', parts[1]


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _processing_order(names, priority_authors):
    """Change traversal only; preserve selection/report order and existing checkpoints."""
    priority = list(dict.fromkeys(name.strip().lower().lstrip('@')
                                  for name in priority_authors.split(',') if name.strip()))
    if any(name not in names for name in priority):
        raise ValueError('Некорректный приоритет авторов')
    prioritized = set(priority)
    return priority + [name for name in names if name not in prioritized]


def _item(raw, username, followers, collected_at, start, end):
    if not isinstance(raw, dict) or raw.get('media_type') != 'CAROUSEL_ALBUM':
        return None
    identity = raw.get('id')
    stamp = _stamp(raw.get('timestamp'))
    if identity is None or not stamp or not start <= stamp < end:
        return None
    permalink, shortcode = _permalink(raw.get('permalink'))
    caption, redactions = _caption(raw.get('caption'))
    return {
        'id': str(identity), 'username': username, 'permalink': permalink,
        'shortcode': shortcode, 'timestamp': stamp.isoformat(),
        'caption': caption, 'redactions': redactions,
        'media_type': 'CAROUSEL_ALBUM', 'media_url': _media_url(raw.get('media_url')),
        'thumbnail_url': _media_url(raw.get('thumbnail_url')),
        'likes': _count(raw.get('like_count')), 'comments': _count(raw.get('comments_count')),
        'views': None, 'followers': _count(followers),
        'collected_at': collected_at,
    }


def _report(selection, names, state, status, clock):
    authors = state['authors']
    available = [name for name in names if authors.get(name, {}).get('status') == 'available']
    unavailable = [name for name in names if authors.get(name, {}).get('status') == 'unavailable']
    items = []
    for name in names:
        details = authors.get(name, {}).get('details', {})
        if isinstance(details, dict):
            items.extend(value for value in details.values() if isinstance(value, dict))
    distinct = {(item['username'], item['id']): item for item in items}
    items = sorted(distinct.values(), key=lambda item: (item['timestamp'], item['id']), reverse=True)
    profiles = [{'username': name,
                 'carousels': sum(item['username'] == name for item in items)
                 if name in available else None} for name in names]
    return {
        'start_date': selection['start_date'], 'end_date': selection['end_date'],
        'timezone': selection['timezone'], 'collected_at': _now(clock).isoformat(),
        'provider': 'meta_graph', 'paid_runs': 0,
        'collection': {
            'status': status, 'target': len(names), 'checked': len(available) + len(unavailable),
            'available': len(available), 'unavailable': len(unavailable),
            'pending': len(names) - len(available) - len(unavailable),
            'requests': state['requests'], 'cooldown_until': state.get('cooldown_until'),
            'review_required': sum(bool(authors.get(name, {}).get('needs_review')) for name in names),
            'redactions': sum(item.get('redactions', 0) for item in items),
        },
        'totals': {'carousels': len({item['id'] for item in items})},
        'profiles': profiles, 'items': items,
    }


def run_batch(checkpoint_dir, output_report, *, token=None, account_id=None, api_version='v23.0',
              max_seconds=480, max_requests=20, client=None, clock=None, priority_authors=''):
    """Сделать bounded batch. Состояние пишется до HTTP и после каждой страницы."""
    checkpoint_dir, output_report = Path(checkpoint_dir), Path(output_report)
    clock = clock or (lambda: datetime.now(timezone.utc))
    selection, names, start = _selection(checkpoint_dir)
    processing_names = _processing_order(names, priority_authors)
    end = start + timedelta(days=(datetime.fromisoformat(selection['end_date']).date()
                                   - datetime.fromisoformat(selection['start_date']).date()).days + 1)
    state_path = checkpoint_dir / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if state.get('job_kind') not in (None, 'carousels_week') or any(
            'items' in author for author in state.get('authors', {}).values()):
        raise ValueError('Checkpoint другого сбора нельзя использовать для каруселей')
    state['job_kind'] = 'carousels_week'
    state.setdefault('authors', {})
    state.setdefault('requests', 0)
    state.setdefault('started_at', _now(clock).isoformat())
    deadline = _now(clock) + timedelta(seconds=max_seconds)
    attempts = 0

    def publish(status):
        state['job_status'] = status
        atomic_json(state_path, state)
        result = _report(selection, names, state, status, clock)
        atomic_json(output_report, result)
        return result

    def cool(seconds):
        state['cooldown_until'] = (_now(clock) + timedelta(seconds=seconds)).isoformat()
        return publish('cooldown')

    future = _stamp(state.get('cooldown_until'))
    if future and future > _now(clock):
        return publish('cooldown')
    state.pop('cooldown_until', None)
    if not token or not account_id:
        return publish('authentication_required')
    if client is None:
        raise ValueError('Нужен HTTP-клиент')
    if not re.fullmatch(r'v\d+\.\d+', api_version) or not str(account_id).isdigit():
        return publish('permissions_or_request_error')
    url = f'https://graph.facebook.com/{api_version}/{account_id}'

    for username in processing_names:
        author = state['authors'].setdefault(username, {})
        if author.get('status') == 'unavailable' or (author.get('scan_complete') and not author.get('detail_pages')):
            continue
        author.setdefault('details', {})
        author.setdefault('detail_pages', [])
        author.setdefault('next_page', 1)
        author.setdefault('seen_cursors', [])
        author.setdefault('traversal_ids', [])
        if author['next_page'] > 1 and not author.get('after') and not author.get('scan_complete'):
            # Неполный checkpoint без курсора: повторяем начало, сохраняя детали.
            author.update(next_page=1, after=None, seen_cursors=[], traversal_ids=[])
        while not author.get('scan_complete') or author['detail_pages']:
            remaining = (deadline - _now(clock)).total_seconds()
            if attempts >= max_requests or remaining <= 0:
                return publish('running')
            # Детали читаем сразу после страницы с каруселью: короткий пилот
            # успеет сохранить карточки, даже если лента автора длинная.
            detail = bool(author['detail_pages'])
            task = author['detail_pages'][0] if detail else None
            after = task['after'] if detail else author.get('after')
            cursor = f'.after({after})' if after else ''
            media_fields = DETAIL_FIELDS if detail else SCAN_FIELDS
            root_fields = 'followers_count,' if detail else 'media_count,'
            fields = (f'business_discovery.username({username}){{{root_fields}'
                      f'media.limit(25){cursor}{{{media_fields}}}}}')
            attempts += 1
            state['requests'] += 1
            atomic_json(state_path, state)
            try:
                response = client.get(url, headers={'Authorization': f'Bearer {token}'},
                                      params={'fields': fields}, timeout=min(45, remaining))
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError('Некорректный ответ')
            except Exception:  # noqa: BLE001 — HTTP-клиент может выбрасывать разные сетевые исключения.
                author['reason'] = 'network_error'
                return cool(120)
            error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
            code = error.get('code')
            usage = _usage(response.headers)
            state['last_usage'] = usage
            if response.status_code == 429 or code in LIMIT_CODES or response.headers.get('retry-after'):
                author['reason'] = 'rate_limit'
                return cool(max(900, _retry_after(response.headers, _now(clock))))
            if code == 190:
                author['reason'] = 'authentication_error'
                return publish('authentication_required')
            if code in (10, 200) or any(word in str(error.get('message', '')).lower()
                                         for word in ('nonexisting field', 'unknown field')):
                author['reason'] = 'permissions_or_request_error'
                return publish('permissions_or_request_error')
            if response.status_code >= 500 or error.get('is_transient'):
                author['reason'] = 'temporary_error'
                return cool(300)
            bd = payload.get('business_discovery')
            if error or response.status_code >= 400 or not isinstance(bd, dict):
                if detail or author.get('status') == 'available':
                    author.update(needs_review=True, reason='continuation_unavailable', scan_complete=True)
                    if detail:
                        author['detail_pages'].pop(0)
                else:
                    author.update(status='unavailable', reason='profile_unavailable', scan_complete=True)
                publish('running')
                break
            media = bd.get('media')
            if media is None and bd.get('media_count') == 0:
                media = {'data': []}
            if not isinstance(media, dict) or not isinstance(media.get('data'), list):
                author['reason'] = 'incomplete_response'
                return publish('failed')
            batch = media['data']
            if detail:
                found = set()
                followers = bd.get('followers_count')
                for raw in batch:
                    if not isinstance(raw, dict) or str(raw.get('id')) not in task['ids']:
                        continue
                    found.add(str(raw['id']))
                    item = _item(raw, username, followers, _now(clock).isoformat(), start, end)
                    if item:
                        author['details'][item['id']] = item
                if found != set(task['ids']):
                    author.update(needs_review=True, reason='detail_page_changed')
                author['detail_pages'].pop(0)
            else:
                author['status'] = 'available'
                paging = media.get('paging') if isinstance(media.get('paging'), dict) else {}
                cursors = paging.get('cursors') if isinstance(paging.get('cursors'), dict) else {}
                after_next = cursors.get('after')
                page_ids = [str(raw['id']) for raw in batch
                            if isinstance(raw, dict) and raw.get('id') is not None]
                seen = set(author['seen_cursors'])
                traversed = set(author['traversal_ids'])
                repeated_cursor = bool(after_next and after_next in seen)
                repeated_page = bool(batch and len(page_ids) == len(batch) and author['next_page'] > 1
                                     and all(identity in traversed for identity in page_ids))
                candidate_ids = []
                queued_ids = {identity for queued in author['detail_pages']
                              for identity in queued['ids']}
                for raw in batch:
                    if not isinstance(raw, dict) or raw.get('media_type') != 'CAROUSEL_ALBUM':
                        continue
                    stamp = _stamp(raw.get('timestamp'))
                    identity = raw.get('id')
                    if identity is not None and stamp and start <= stamp < end:
                        identity = str(identity)
                        if (identity not in author['details'] and identity not in queued_ids
                                and identity not in candidate_ids):
                            candidate_ids.append(identity)
                if candidate_ids:
                    author['detail_pages'].append({'after': after, 'ids': candidate_ids})
                if repeated_cursor or repeated_page:
                    author.update(scan_complete=True, needs_review=True, reason='pagination_stalled')
                else:
                    author['traversal_ids'] = sorted(traversed.union(page_ids))
                    if isinstance(after_next, str) and after_next:
                        author['seen_cursors'].append(after_next)
                    stamps = [_stamp(raw.get('timestamp')) for raw in batch if isinstance(raw, dict)]
                    stamps = [stamp for stamp in stamps if stamp]
                    old_tail = len(stamps) >= 5 and all(stamp < start for stamp in stamps[-5:])
                    finished = not after_next or not batch or old_tail
                    author.update(scan_complete=finished, after=after_next,
                                  next_page=author['next_page'] + 1,
                                  coverage='complete' if not after_next else 'partial',
                                  reason='cursor_exhausted' if not after_next else
                                         'empty_page' if not batch else
                                         'observed_window' if old_tail else 'continuing')
            publish('running')
            if max(usage.values(), default=0) >= 85:
                return cool(600)
    completed = all(state['authors'].get(name, {}).get('status') in ('available', 'unavailable')
                    and state['authors'][name].get('scan_complete')
                    and not state['authors'][name].get('detail_pages') for name in names)
    return publish('finished' if completed else 'running')
