"""Возобновляемый пакетный сбор метаданных Instagram Business Discovery."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.radar.activity import build_activity_report


LIMIT_CODES = {4, 17, 32, 613, 80002}
USAGE_FIELDS = ('call_count', 'total_cputime', 'total_time')
USERNAME = re.compile(r'[a-zA-Z0-9._]{1,30}\Z')


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _now(clock):
    value = clock()
    if value.tzinfo is None:
        raise ValueError('clock должен возвращать aware datetime')
    return value.astimezone(timezone.utc)


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        try:
            return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S%z')
        except (TypeError, ValueError):
            return None


def _items_unique(items):
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = item.get('id') or json.dumps(item, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _usage(headers):
    try:
        value = json.loads(headers.get('x-app-usage', '{}'))
    except (TypeError, ValueError):
        return {}
    return {field: value[field] for field in USAGE_FIELDS
            if isinstance(value, dict) and isinstance(value.get(field), (int, float))
            and not isinstance(value[field], bool)}


def _retry_after(headers, now):
    value = headers.get('retry-after')
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(value)
            return max(0, int((stamp - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def _selection(checkpoint_dir):
    value = json.loads((checkpoint_dir / 'selection.json').read_text(encoding='utf-8'))
    names = [entry['username'] for entry in value['authors']]
    if not names or len(names) != len(set(names)) or any(not USERNAME.fullmatch(u) for u in names):
        raise ValueError('Некорректный список авторов')
    first, last = date.fromisoformat(value['start_date']), date.fromisoformat(value['end_date'])
    if last < first or value.get('total', len(names)) != len(names):
        raise ValueError('Некорректный период или размер выборки')
    ZoneInfo(value['timezone'])
    return value, names, datetime.combine(first, datetime.min.time(), ZoneInfo(value['timezone']))


def _report(selection, names, state, status, clock):
    authors = state['authors']
    available = [u for u in names if authors.get(u, {}).get('status') == 'available']
    unavailable = [u for u in names if authors.get(u, {}).get('status') == 'unavailable']
    review_required = sum(bool(authors.get(u, {}).get('needs_review')) for u in names)
    posts, coverage = [], {}
    for username in available:
        author = authors[username]
        quality = author.get('coverage', 'partial')
        coverage[username] = {'posts': quality, 'reels': quality}
        for item in _items_unique(author.get('items', [])):
            stamp = _stamp(item.get('timestamp'))
            posts.append({'id': item.get('id'), 'timestamp': stamp.isoformat() if stamp else None,
                          'ownerUsername': username, 'inputUrl': f'https://www.instagram.com/{username}/',
                          'productType': 'clips' if item.get('media_product_type') == 'REELS' else None,
                          'type': {'IMAGE': 'Image', 'CAROUSEL_ALBUM': 'Sidecar'}.get(item.get('media_type'), 'Video')})
    report = build_activity_report(available, posts, [], coverage,
                                   start_date=selection['start_date'], end_date=selection['end_date'],
                                   timezone=selection['timezone'])
    report.pop('diagnostics', None)
    report.update(provider='meta_graph', paid_runs=0, collected_at=_now(clock).isoformat(),
                  collection={'status': status, 'target': len(names), 'checked': len(available) + len(unavailable),
                              'available': len(available), 'unavailable': len(unavailable),
                              'review_required': review_required,
                              'pending': len(names) - len(available) - len(unavailable),
                              'requests': state['requests'], 'cooldown_until': state.get('cooldown_until'),
                              'selection_label': selection.get('label', 'Все авторы каталога')})
    return report


def run_batch(checkpoint_dir, output_report, *, token=None, account_id=None, api_version='v23.0',
              max_seconds=480, max_requests=20, client=None, clock=None):
    """Сделать ограниченный batch; клиент имеет метод get(url, headers, params)."""
    checkpoint_dir, output_report = Path(checkpoint_dir), Path(output_report)
    clock = clock or (lambda: datetime.now(timezone.utc))
    selection, names, since = _selection(checkpoint_dir)
    state_path = checkpoint_dir / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    state.setdefault('authors', {})
    state.setdefault('requests', 0)
    state.setdefault('started_at', _now(clock).isoformat())
    deadline = _now(clock) + timedelta(seconds=max_seconds)
    attempts = 0

    def publish(status):
        state['job_status'] = status
        atomic_json(state_path, state)
        report = _report(selection, names, state, status, clock)
        atomic_json(output_report, report)
        return report

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

    for username in names:
        author = state['authors'].get(username, {})
        if author.get('status') in ('available', 'unavailable') and not author.get('resume_required'):
            continue
        items = _items_unique(author.get('items', []))
        if author.get('resume_required'):
            page = author.get('next_page')
            after = author.get('after')
            if (not isinstance(page, int) or isinstance(page, bool) or page < 1
                    or (page > 1 and not after)):
                # Старый неполный checkpoint восстанавливаем с первой страницы.
                # Собранные items сохраняются и дедуплицируются при продолжении.
                page, after = 1, None
        else:
            page, after = 1, None
        # При восстановлении с первой страницы старая история обхода не годится
        # для поиска циклов: сохранённые items нужны лишь для итоговой дедупликации.
        restarting = page == 1 and not after
        if restarting:
            author = dict(author, next_page=1, after=None, seen_cursors=[], traversal_ids=[])
            state['authors'][username] = author
        seen_cursors = [cursor for cursor in author.get('seen_cursors', [])
                        if isinstance(cursor, str) and cursor]
        traversal_ids = {str(identity) for identity in author.get('traversal_ids', [])}
        if isinstance(after, str) and after and after not in seen_cursors:
            seen_cursors.append(after)

        while True:
            remaining = (deadline - _now(clock)).total_seconds()
            if attempts >= max_requests or remaining <= 0:
                return publish('running')
            cursor = f'.after({after})' if after else ''
            fields = (f'business_discovery.username({username}){{username,media_count,'
                      f'media.limit(25){cursor}{{id,media_type,media_product_type,timestamp}}}}')
            attempts += 1
            state['requests'] += 1
            atomic_json(state_path, state)  # Попытка учитывается до HTTP, включая падение процесса.
            try:
                response = client.get(url, headers={'Authorization': f'Bearer {token}'},
                                      params={'fields': fields}, timeout=min(45, remaining))
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError('Некорректный ответ')
            except Exception:
                if items:
                    author.update(status='available', coverage='partial', items=items,
                                  reason='continuation_network_error', resume_required=True,
                                  next_page=page, after=after)
                else:
                    author = {'status': 'pending', 'reason': 'network_error', 'resume_required': True,
                              'next_page': page, 'after': after, 'items': []}
                state['authors'][username] = author
                return cool(120)

            error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
            code = error.get('code')
            usage = _usage(response.headers)
            state['last_usage'] = usage
            if response.status_code == 429 or code in LIMIT_CODES or response.headers.get('retry-after'):
                if items:
                    author.update(status='available', coverage='partial', items=items,
                                  reason='continuation_rate_limit', resume_required=True,
                                  next_page=page, after=after)
                else:
                    author = {'status': 'pending', 'reason': 'rate_limit', 'resume_required': True,
                              'next_page': page, 'after': after, 'items': []}
                state['authors'][username] = author
                return cool(max(900, _retry_after(response.headers, _now(clock))))
            if code == 190:
                status, reason = 'authentication_required', 'authentication_error'
            elif code in (10, 200) or any(s in str(error.get('message', '')).lower()
                                            for s in ('nonexisting field', 'unknown field')):
                status, reason = 'permissions_or_request_error', 'permissions_or_request_error'
            else:
                status = reason = None
            if status:
                if items:
                    author.update(status='available', coverage='partial', items=items,
                                  reason='continuation_' + reason, resume_required=True,
                                  next_page=page, after=after)
                else:
                    author = {'status': 'pending', 'reason': reason, 'resume_required': True,
                              'next_page': page, 'after': after, 'items': []}
                state['authors'][username] = author
                return publish(status)
            if response.status_code >= 500 or error.get('is_transient'):
                author = dict(author, status='available' if items else 'pending', coverage='partial',
                              items=items, reason='temporary_error', resume_required=True,
                              next_page=page, after=after)
                state['authors'][username] = author
                return cool(300)
            bd = payload.get('business_discovery')
            if error or response.status_code >= 400 or not isinstance(bd, dict):
                if items:
                    author = dict(author, status='available', coverage='partial', items=items,
                                  reason='continuation_unavailable', resume_required=False)
                else:
                    author = dict(author, status='unavailable', reason='profile_unavailable',
                                  items=[], resume_required=False)
                state['authors'][username] = author
                publish('running')
                break
            media = bd.get('media')
            if media is None and bd.get('media_count') == 0:
                media = {'data': []}
            if not isinstance(media, dict) or not isinstance(media.get('data'), list):
                author = dict(author, status='available' if items else 'pending', coverage='partial',
                              items=items, reason='incomplete_response', resume_required=True,
                              next_page=page, after=after)
                state['authors'][username] = author
                return publish('failed')
            batch = media['data']
            after_next = (media.get('paging', {}).get('cursors') or {}).get('after') if isinstance(media.get('paging'), dict) else None
            page_ids = [str(entry['id']) for entry in batch
                        if isinstance(entry, dict) and entry.get('id') is not None]
            repeated_page = bool(batch and len(page_ids) == len(batch) and page > 1
                                 and all(identity in traversal_ids for identity in page_ids))
            repeated_cursor = bool(after_next and after_next in seen_cursors)
            items = _items_unique(items + batch)
            if repeated_cursor or repeated_page:
                author = dict(author, status='available', coverage='partial', items=items,
                              reason='pagination_stalled', needs_review=True,
                              resume_required=False, next_page=page + 1, after=after_next,
                              seen_cursors=seen_cursors, traversal_ids=sorted(traversal_ids))
                state['authors'][username] = author
                publish('running')
                break
            traversal_ids.update(page_ids)
            if isinstance(after_next, str) and after_next:
                seen_cursors.append(after_next)
            stamps = [_stamp(item.get('timestamp')) for item in batch if isinstance(item, dict)]
            stamps = [stamp for stamp in stamps if stamp is not None]
            old_tail = len(stamps) >= 5 and all(stamp < since for stamp in stamps[-5:])
            exhausted = not after_next
            finished = exhausted or batch == [] or old_tail
            reason = ('cursor_exhausted' if exhausted else
                      'empty_page' if batch == [] else 'observed_window')
            author = dict(author, status='available', coverage='complete' if exhausted else 'partial',
                          items=items, reason=reason,
                          resume_required=not finished, next_page=page + 1, after=after_next,
                          seen_cursors=seen_cursors, traversal_ids=sorted(traversal_ids))
            state['authors'][username] = author
            publish('running')
            if max(usage.values(), default=0) >= 85:
                return cool(600)
            if finished:
                break
            page += 1
            after = after_next
    completed = all(state['authors'].get(u, {}).get('status') in ('available', 'unavailable')
                    and not state['authors'].get(u, {}).get('resume_required') for u in names)
    return publish('finished' if completed else 'running')
