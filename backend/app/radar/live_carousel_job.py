"""Ограниченный возобновляемый обход общего избранного через Meta."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from app.radar.activity_job import LIMIT_CODES, USERNAME, _retry_after, _stamp, _usage
from app.radar.carousel_job import DETAIL_FIELDS, _item
from app.radar.carousel_window import history_window, rolling_window


HISTORY_MONTHS = 4
COVERAGE_VERSION = 3


def window(now, days=None):
    return history_window(now) if days is None else rolling_window(now, days)


def _run_lane(store, client, *, token, account_id, api_version='v23.0', max_requests=30, clock=None, lane='history'):
    """Одна сессия Actions; длительность ограничена числом страниц и timeout HTTP."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    if not token or not str(account_id).isdigit() or not re.fullmatch(r'v\d+\.\d+', api_version):
        raise ValueError('Нет корректных настроек Meta')
    start, end = window(now)
    authors = store.authors()
    pauses = [_stamp(row.get('next_check_at')) for row in authors if row.get('status') == 'meta_paused']
    if any(stamp and stamp > now for stamp in pauses):
        return {'requests': 0, 'status': 'meta_paused'}
    attempt_field = 'history_last_attempt_at' if lane == 'history' else 'recent_last_attempt_at'
    authors.sort(key=lambda row: (row.get(attempt_field) or '', row.get('added_at', ''), row['username']))
    requests = 0
    unfinished = False
    for row in authors:
        username = row['username']
        needs_history = row.get('history_months') != HISTORY_MONTHS or row.get('coverage_version') != COVERAGE_VERSION or row.get('history_status') != 'complete'
        if lane == 'history' and not needs_history:
            continue
        due = _stamp(row.get('history_next_check_at' if lane == 'history' else 'next_check_at'))
        if (due and due > now) or not USERNAME.fullmatch(username):
            continue
        checkpoint_field = 'history_checkpoint' if lane == 'history' else 'checkpoint'
        checkpoint = dict(row.get(checkpoint_field) or {})
        if checkpoint.get('version') != COVERAGE_VERSION or checkpoint.get('lane') != lane:
            checkpoint = {}
        checkpoint.update(version=COVERAGE_VERSION, history_months=HISTORY_MONTHS, lane=lane)
        previous = _stamp(row.get('last_checked_at'))
        # Догрузка сохраняет первоначальную границу и курсор, новые материалы
        # независимо перечитываются с перекрытием в сутки.
        scan_start = start if lane == 'history' else (previous or now) - timedelta(days=1)
        checkpoint.setdefault('start', scan_start.isoformat())
        checkpoint.setdefault('started_at', now.isoformat())
        checkpoint.setdefault('seen', [])
        checkpoint.setdefault('ids', [])
        scan_start = _stamp(checkpoint['start']) or scan_start
        author_requests = 0
        author_unfinished = True
        def update(*, _username=username, **values):
            if lane == 'history':
                mapped = {}
                for key, value in values.items():
                    target = {'status': 'history_status', 'error': 'history_error',
                              'checkpoint': 'history_checkpoint', 'next_check_at': 'history_next_check_at',
                              'last_checked_at': 'history_to'}.get(key, key)
                    mapped[target] = 'complete' if key == 'status' and value == 'updated' else value
                if mapped.get('history_status') == 'complete':
                    mapped['history_from'] = scan_start.isoformat()
                    mapped['history_to'] = checkpoint['started_at']
                phase = mapped.get('history_status')
                mapped['status'] = ('updated' if phase == 'complete' and row.get('last_checked_at') and not row.get('checkpoint') and not row.get('error') else
                                    'unavailable' if phase == 'unavailable' else
                                    'error' if phase == 'error' else 'collecting')
                values = mapped
            elif values.get('status') == 'updated' and needs_history:
                values['status'] = 'collecting'
            store.update(_username, expected_generation=row.get('tracking_generation'), last_attempt_at=clock().isoformat(),
                         **{attempt_field: clock().isoformat()}, **values)

        def pause(until, reason, _username=username):
            store.pause(until, reason, username=_username, last_attempt_at=clock().isoformat())

        while requests < max_requests and author_requests < 5:
            if not store.active(username):
                break
            if hasattr(store, 'can_request_meta') and not store.can_request_meta():
                return {'requests': requests, 'status': 'running'}
            after = checkpoint.get('after')
            if after and not re.fullmatch(r'[A-Za-z0-9_\-=/+]+', after):
                update(status='error', error='invalid_cursor', checkpoint={},
                             next_check_at=(now + timedelta(hours=1)).isoformat())
                break
            media = 'media.limit(50)' + (f'.after({after})' if after else '') + '{' + DETAIL_FIELDS + '}'
            fields = f'business_discovery.username({username}){{followers_count,{media}}}'
            requests += 1
            author_requests += 1
            try:
                response = client.get(f'https://graph.facebook.com/{api_version}/{account_id}',
                    headers={'Authorization': f'Bearer {token}'}, params={'fields': fields}, timeout=45)
                payload = response.json()
            except (httpx.TransportError, ValueError):
                update(status='error', error='temporary_transport_error',
                             next_check_at=(now + timedelta(minutes=15)).isoformat())
                break
            error = payload.get('error', {}) if isinstance(payload, dict) else {}
            code = error.get('code')
            usage = max(_usage(response.headers).values(), default=0)
            if response.status_code == 429 or code in LIMIT_CODES or code in (190, 10, 200, 294):
                seconds = max(3600 if code in (190, 10, 200, 294) else 900, _retry_after(response.headers, now))
                pause((now + timedelta(seconds=seconds)).isoformat(),
                            'authentication_required' if code == 190 else
                            'permissions_required' if code in (10, 200, 294) else 'meta_rate_limit')
                return {'requests': requests, 'status': 'meta_paused'}
            discovery = payload.get('business_discovery') if isinstance(payload, dict) else None
            if usage >= 85 and (error or response.status_code >= 400 or not isinstance(discovery, dict)):
                pause((now + timedelta(minutes=15)).isoformat(), 'meta_usage_85')
                return {'requests': requests, 'status': 'meta_paused'}
            if response.status_code >= 500:
                update(status='error', error='temporary_meta_error',
                             next_check_at=(now + timedelta(minutes=15)).isoformat())
                break
            if error or not isinstance(discovery, dict):
                update(status='unavailable', error='profile_unavailable', checkpoint={},
                             next_check_at=(now + timedelta(hours=6)).isoformat())
                break
            page = discovery.get('media', {})
            batch = page.get('data') if isinstance(page, dict) else None
            if not isinstance(batch, list):
                update(status='error', error='invalid_meta_response',
                             next_check_at=(now + timedelta(minutes=15)).isoformat())
                break
            items = [_item(raw, username, discovery.get('followers_count'), now.isoformat(), scan_start, end)
                     for raw in batch]
            store.save(list({item['id']: item for item in items if item}.values()))
            cursor = page.get('paging', {}).get('cursors', {}).get('after')
            ids = [str(raw['id']) for raw in batch if isinstance(raw, dict) and 'id' in raw]
            repeated = bool(cursor and cursor in checkpoint['seen']) or bool(ids and set(ids) <= set(checkpoint['ids']))
            stamps = [_stamp(raw.get('timestamp')) for raw in batch if isinstance(raw, dict)]
            stamps = [stamp for stamp in stamps if stamp]
            old_tail = len(stamps) >= 5 and all(stamp < scan_start for stamp in stamps[-5:])
            finished = not cursor or not batch or old_tail
            if repeated:
                values = {'status': 'error', 'error': 'pagination_stalled', 'checkpoint': {},
                          'next_check_at': (now + timedelta(hours=1)).isoformat()}
            elif finished:
                values = {'status': 'updated', 'error': None, 'checkpoint': {},
                          'last_checked_at': checkpoint['started_at'],
                          **({'history_months': HISTORY_MONTHS, 'history_days': (end - scan_start).days, 'coverage_version': COVERAGE_VERSION} if lane == 'history' else {}),
                          'next_check_at': (now + timedelta(minutes=60)).isoformat()}
            else:
                checkpoint.update(after=cursor, seen=checkpoint['seen'] + [cursor],
                                  ids=list(set(checkpoint['ids']).union(ids)))
                values = {'status': 'collecting', 'error': None, 'checkpoint': checkpoint, 'next_check_at': None}
            if usage >= 85:
                values.update(pause_until=(now + timedelta(minutes=15)).isoformat(), pause_reason='meta_usage_85')
            update(**values)
            if usage >= 85:
                return {'requests': requests, 'status': 'meta_paused'}
            if finished or repeated:
                author_unfinished = False
                break
        unfinished = unfinished or author_unfinished
        if requests >= max_requests:
            break
    return {'requests': requests, 'status': 'running' if requests >= max_requests or unfinished else 'finished'}


def run(store, client, *, token, account_id, api_version='v23.0', max_requests=30, clock=None):
    """Резерв для новых материалов; оставшийся бюджет — историческому обходу."""
    if max_requests < 1:
        return {'requests': 0, 'status': 'running'}
    kwargs = dict(token=token, account_id=account_id, api_version=api_version, clock=clock)
    recent_budget = max(1, max_requests // 2)
    recent = _run_lane(store, client, lane='recent', max_requests=recent_budget, **kwargs)
    if recent['status'] == 'meta_paused':
        return recent
    remaining = max_requests - recent['requests']
    if remaining <= 0:
        return {'requests': recent['requests'], 'status': 'running'}
    history = _run_lane(store, client, lane='history', max_requests=remaining, **kwargs)
    return {'requests': recent['requests'] + history['requests'],
            'status': 'meta_paused' if history['status'] == 'meta_paused' else
                      'running' if 'running' in (recent['status'], history['status']) else 'finished'}
