"""Чистый подсчёт публикаций: без сети, БД и изменения источников."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone as utc_timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def _username(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lstrip('@').lower()
    return value if re.fullmatch(r'[a-z0-9._]{1,30}', value) else None


def _instagram_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme in ('https', 'http') and parts.hostname in
                ('instagram.com', 'www.instagram.com', 'm.instagram.com')
                and not parts.username and not parts.password):
            return parts
    except ValueError:
        pass
    return None


def _input_profile(value):
    parts = _instagram_url(value)
    path = parts.path.strip('/').split('/') if parts else []
    if len(path) == 1 and path[0].lower() not in ('p', 'reel', 'reels', 'explore', 'stories'):
        return _username(path[0])
    return None


def normalize_activity_item(item: dict, source_stream: str, usernames: list[str],
                            timezone: str = 'Europe/Moscow') -> dict:
    """Вернуть {record, reason, profile}; диагностические причины не содержат сырья."""
    selected = set(usernames)
    if source_stream not in ('posts', 'reels'):
        raise ValueError('Неизвестный поток публикаций')
    if not isinstance(item, dict):
        return {'record': None, 'reason': 'invalid_item', 'profile': None}
    requested = _input_profile(item.get('inputUrl'))
    owner = _username(item.get('ownerUsername'))
    profile = requested if requested in selected else owner
    if requested and requested not in selected:
        return {'record': None, 'reason': 'unselected_profile', 'profile': requested}
    if profile not in selected:
        return {'record': None, 'reason': 'unselected_profile' if profile else 'missing_profile', 'profile': profile}
    if item.get('isChild') or item.get('parentId') or item.get('parentShortCode'):
        return {'record': None, 'reason': 'carousel_child', 'profile': profile}
    stamp = item.get('timestamp')
    try:
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            moment = datetime.fromtimestamp(stamp, utc_timezone.utc)
        elif isinstance(stamp, str):
            moment = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                raise ValueError('Дата без часового пояса')
        else:
            raise ValueError('Нет даты')
        day = moment.astimezone(ZoneInfo(timezone)).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return {'record': None, 'reason': 'invalid_timestamp', 'profile': profile}
    parts = _instagram_url(item.get('url'))
    path = parts.path.strip('/').split('/') if parts else []
    shortcode = item.get('shortCode')
    shortcode = str(shortcode).strip() if shortcode is not None else ''
    if not shortcode and len(path) >= 2 and path[0] in ('p', 'reel', 'reels'):
        shortcode = path[1]
    platform_id = item.get('id')
    platform_id = str(platform_id).strip() if platform_id is not None else ''
    identifiers = ([f'id:{platform_id}'] if platform_id else []) + ([f'code:{shortcode}'] if shortcode else [])
    if not identifiers:
        return {'record': None, 'reason': 'missing_identity', 'profile': profile}
    is_reel = source_stream == 'reels' or item.get('productType') == 'clips' or bool(path and path[0] in ('reel', 'reels'))
    kind = 'reels' if is_reel else {'Sidecar': 'carousels', 'Image': 'posts'}.get(item.get('type'), 'unknown')
    return {'record': {'profile': profile, 'owner_username': owner, 'day': day,
                       'identifiers': identifiers, 'kind': kind,
                       'source_stream': source_stream,
                       'url': parts.geturl() if parts else None},
            'reason': None, 'profile': profile}


def build_activity_report(usernames: list[str], posts: list[dict], reels: list[dict],
                          coverage: dict[str, dict], start_date: str = '2026-10-15',
                          end_date: str = '2026-10-17', timezone: str = 'Europe/Moscow') -> dict:
    """Дедупликация контента и отдельный учёт размещений в выбранных профилях.

    observed — подтверждённые наблюдения (нижняя граница); exact/avg доступны
    только при полном покрытии обоих потоков без ошибок нормализации.
    """
    ZoneInfo(timezone)
    first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if last < first:
        raise ValueError('Конец периода раньше начала')
    selected = []
    for value in usernames:
        username = _username(value)
        if username is None:
            raise ValueError('Некорректный username')
        if username not in selected:
            selected.append(username)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    states = ('complete', 'partial', 'error', 'not_checked')
    checked = {u: {s: coverage.get(u, {}).get(s, 'not_checked') for s in ('posts', 'reels')} for u in selected}
    for streams in checked.values():
        for stream, status in streams.items():
            if status not in states:
                streams[stream] = 'not_checked'
    records, diagnostics = [], []
    # Сначала собираем все идентификаторы, в том числе за пределами периода:
    # третья запись может связать id и shortCode двух предыдущих записей.
    for stream, items in (('posts', posts), ('reels', reels)):
        for index, item in enumerate(items):
            result = normalize_activity_item(item, stream, selected, timezone)
            if result['record']:
                records.append(result['record'])
            else:
                diagnostics.append({'stream': stream, 'index': index,
                                    'reason': result['reason'], 'profile': result['profile']})
                if result['reason'] not in ('unselected_profile', 'carousel_child'):
                    affected = [result['profile']] if result['profile'] in checked else selected
                    for username in affected:
                        if checked[username][stream] == 'complete':
                            checked[username][stream] = 'partial'
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for record in records:
        ids = record['identifiers']
        for ident in ids[1:]:
            parent[find(ident)] = find(ids[0])
    grouped = {}
    for record in records:
        grouped.setdefault(find(record['identifiers'][0]), []).append(record)

    def blank():
        return dict(observed_total=0, posts=0, reels=0, carousels=0, unknown=0)

    per_day = {u: {day: blank() for day in days} for u in selected}
    unique_day = {day: blank() for day in days}
    priority = {'unknown': 0, 'posts': 1, 'carousels': 2, 'reels': 3}
    for group in grouped.values():
        observed_days = {r['day'] for r in group}
        if len(observed_days) > 1:
            diagnostics.append({'reason': 'conflicting_dates', 'profiles': sorted({r['profile'] for r in group})})
            for r in group:
                if checked[r['profile']][r['source_stream']] == 'complete':
                    checked[r['profile']][r['source_stream']] = 'partial'
        # При противоречии дат используем раннюю, явно помечая точность неполной.
        day = min(observed_days)
        if day not in unique_day:
            continue
        kind = max((r['kind'] for r in group), key=priority.get)
        unique_day[day]['observed_total'] += 1
        unique_day[day][kind] += 1
        for username in {r['profile'] for r in group}:
            per_day[username][day]['observed_total'] += 1
            per_day[username][day][kind] += 1
    complete = {u: all(v == 'complete' for v in checked[u].values()) for u in selected}
    completed = sum(complete.values())

    def total(rows):
        return {key: sum(row[key] for row in rows) for key in blank()}

    profiles = []
    for username in selected:
        rows = []
        for day in days:
            row = per_day[username][day]
            rows.append(dict(date=day, **row, exact_total=row['observed_total'] if complete[username] else None))
        sums = total(rows)
        profiles.append(dict(username=username, coverage=checked[username], complete=complete[username], days=rows,
                             **sums, exact_total=sums['observed_total'] if complete[username] else None,
                             avg_per_day=sums['observed_total'] / len(days) if complete[username] else None,
                             observed_avg_per_day=sums['observed_total'] / len(days)))
    daily = []
    for day in days:
        sums = total([per_day[u][day] for u in selected])
        daily.append(dict(date=day, **sums, completed_profiles=completed, total_profiles=len(selected),
                          exact_total=sums['observed_total'] if completed == len(selected) else None,
                          unique_content=dict(**unique_day[day], exact_total=unique_day[day]['observed_total']
                                              if completed == len(selected) else None)))
    sums = total(daily)
    unique = total(list(unique_day.values()))
    return dict(start_date=start_date, end_date=end_date, timezone=timezone, days=daily, profiles=profiles,
                totals=dict(**sums, completed_profiles=completed, total_profiles=len(selected),
                            exact_total=sums['observed_total'] if completed == len(selected) else None,
                            avg_per_day=sums['observed_total'] / len(days) if completed == len(selected) else None,
                            observed_avg_per_day=sums['observed_total'] / len(days),
                            unique_content=dict(**unique, exact_total=unique['observed_total']
                                                if completed == len(selected) else None)),
                diagnostics=diagnostics)
