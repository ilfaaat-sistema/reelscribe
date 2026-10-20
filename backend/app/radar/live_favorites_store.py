"""Общее JSON-хранилище в отдельной ветке GitHub с проверкой версии записи."""
from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from datetime import datetime, timezone

from app.radar.activity_job import USERNAME, _stamp
from app.radar.carousel_job import PUBLIC_SECRET_PATTERNS

REPOSITORY = 'ilfaaat-sistema/reelscribe'
BRANCH = 'data/carousels-favorites-live'
MAX_BYTES = 10 * 1024 * 1024
MAX_AUTHORS = 1000
MARKER = 'Данные живых избранных каруселей; код приложения отсутствует.\n'
STATUSES = {'pending', 'collecting', 'updated', 'meta_paused', 'unavailable', 'error'}
DEFAULT_AUTHORS = ('alex_magnier', 'artemiy.miller', 'ilia.paliy', 'romarayt')


class Conflict(RuntimeError):
    """Ветка изменилась: требуется перечитать данные и повторить изменение."""


def initial_state():
    now = datetime.now(timezone.utc).isoformat()
    return {'version': 1, 'authors': [{'username': name, 'status': 'pending', 'active': True,
            'checkpoint': {}, 'added_at': now, 'last_checked_at': None,
            'next_check_at': None, 'error': None} for name in DEFAULT_AUTHORS], 'items': []}


def validate_state(state):
    if not isinstance(state, dict) or state.get('version') != 1:
        raise ValueError('Неподдерживаемая версия данных')
    authors, items = state.get('authors'), state.get('items')
    if not isinstance(authors, list) or len(authors) > MAX_AUTHORS or not isinstance(items, list):
        raise ValueError('Некорректный размер списка')
    names, ids = set(), set()
    for row in authors:
        if not isinstance(row, dict):
            raise ValueError('Некорректный автор')  # noqa: TRY004 — единый тип ошибки данных.
        name = row.get('username')
        if not isinstance(name, str) or not USERNAME.fullmatch(name) or name != name.lower() or name in names:
            raise ValueError('Некорректное имя автора')
        names.add(name)
        if row.get('status') not in STATUSES or not isinstance(row.get('active'), bool):
            raise ValueError('Некорректное состояние автора')
        if not isinstance(row.get('checkpoint', {}), dict):
            raise ValueError('Некорректная контрольная точка')  # noqa: TRY004 — единый тип ошибки данных.
        for field in ('added_at', 'last_checked_at', 'last_attempt_at', 'next_check_at'):
            if row.get(field) is not None and _stamp(row[field]) is None:
                raise ValueError('Некорректная дата автора')
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
            raise ValueError('Некорректный ID карточки')
        ids.add(item['id'])
        if item.get('username') not in names or _stamp(item.get('timestamp')) is None:
            raise ValueError('Некорректный автор или дата карточки')
        if item.get('media_type') != 'CAROUSEL_ALBUM':
            raise ValueError('Некорректный тип карточки')
    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    # Данные ветки публичны: ключи интеграций и заголовки авторизации запрещены.
    if PUBLIC_SECRET_PATTERNS.search(encoded) or re.search(r'github_pat_[A-Za-z0-9_]{20,}|EAA[A-Za-z0-9]{40,}', encoded) or re.search(
            r'"(?:access_token|authorization|api_key|service_role_key|decrypted_secret)"\s*:', encoded, re.IGNORECASE):
        raise ValueError('В публичных данных обнаружен секрет')
    if len(encoded.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Превышен размер живого отчёта')
    return encoded


class LiveFavoritesStore:
    def __init__(self, client, token=None):
        self.client, self.token = client, token
        self.remaining = None
        self.base = f'https://api.github.com/repos/{REPOSITORY}/'

    def request(self, method, path, *, missing=False, conflict=False, accept='application/vnd.github+json', **kwargs):
        headers = {'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        response = self.client.request(method, self.base + path, headers=headers, timeout=45, **kwargs)
        remaining = response.headers.get('x-ratelimit-remaining')
        if remaining is not None:
            try:
                self.remaining = max(0, int(remaining))
            except ValueError:
                self.remaining = 0
        elif self.remaining is not None:
            self.remaining = max(0, self.remaining - 1)
        if missing and response.status_code == 404:
            return None
        if conflict and response.status_code in (409, 422):
            raise Conflict('Запись изменена параллельно')
        if not 200 <= response.status_code < 300:
            raise RuntimeError('GitHub хранилище недоступно')
        if len(response.content) > MAX_BYTES * 2:
            raise ValueError('Превышен размер ответа хранилища')
        return response.json()

    def load(self):
        ref = self.request('GET', 'git/ref/heads/' + BRANCH, missing=True)
        if ref is None:
            return initial_state(), None
        sha = ref['object']['sha']
        commit = self.request('GET', 'git/commits/' + sha)
        tree = self.request('GET', 'git/trees/' + commit['tree']['sha'])
        if tree.get('truncated') or {row['path'] for row in tree['tree']} != {
                'state.json', 'vercel.json', '.live-carousels-data'}:
            raise ValueError('Ветка содержит чужие файлы')
        entries = {row['path']: row for row in tree['tree']}
        def read_blob(path):
            entry = entries.get(path)
            if not entry or entry.get('type') != 'blob' or entry.get('mode') != '100644':
                raise ValueError('Ветка не принадлежит живым избранным каруселям')
            blob = self.request('GET', 'git/blobs/' + entry['sha'])
            if blob.get('encoding') != 'base64':
                raise ValueError('Неизвестная кодировка хранилища')
            raw = base64.b64decode(blob['content'])
            if len(raw) > MAX_BYTES:
                raise ValueError('Превышен размер живого отчёта')
            return raw
        if read_blob('.live-carousels-data').decode('utf-8') != MARKER:
            raise ValueError('Ветка не принадлежит живым избранным каруселям')
        vercel = json.loads(read_blob('vercel.json'))
        if (vercel != {'git': {'deploymentEnabled': False}}
                or vercel['git']['deploymentEnabled'] is not False):
            raise ValueError('Ветка данных не защищена от деплоя')
        raw = read_blob('state.json')
        state = json.loads(raw)
        validate_state(state)
        return state, sha

    def read_state(self):
        """Быстрое чтение для просмотра; запись всегда идёт через load с marker/CAS."""
        value = self.request('GET', 'contents/state.json?ref=' + BRANCH, missing=True, accept='application/vnd.github.object+json')
        if value is None:
            return initial_state()
        if value.get('type') != 'file':
            raise ValueError('Неизвестный формат живого отчёта')
        if value.get('encoding') == 'base64':
            raw = base64.b64decode(value['content'])
        elif value.get('encoding') == 'none' and value.get('sha'):
            # Contents API после 1 MiB не отдаёт содержимое, но возвращает SHA.
            blob = self.request('GET', 'git/blobs/' + value['sha'])
            if blob.get('encoding') != 'base64':
                raise ValueError('Неизвестная кодировка живого отчёта')
            raw = base64.b64decode(blob['content'])
        else:
            raise ValueError('Неизвестная кодировка живого отчёта')
        if len(raw) > MAX_BYTES:
            raise ValueError('Превышен размер живого отчёта')
        state = json.loads(raw)
        validate_state(state)
        return state

    def save(self, state, expected_sha):
        if not self.token:
            raise ValueError('Для записи нужен серверный GitHub token')
        text = validate_state(state)
        # Новый коммит имеет ровно ожидаемого родителя. non-force update допускает
        # лишь fast-forward: конкурирующие записи становятся разными ветвями.
        tree = self.request('POST', 'git/trees', json={'tree': [
            {'path': 'state.json', 'mode': '100644', 'type': 'blob', 'content': text},
            {'path': 'vercel.json', 'mode': '100644', 'type': 'blob',
             'content': '{"git":{"deploymentEnabled":false}}'},
            {'path': '.live-carousels-data', 'mode': '100644', 'type': 'blob',
             'content': MARKER}]})
        commit = self.request('POST', 'git/commits', json={
            'message': 'Обновить живые избранные карусели', 'tree': tree['sha'],
            'parents': [expected_sha] if expected_sha else []})
        if expected_sha:
            self.request('PATCH', 'git/refs/heads/' + BRANCH,
                         json={'sha': commit['sha'], 'force': False}, conflict=True)
        else:
            self.request('POST', 'git/refs', json={'ref': 'refs/heads/' + BRANCH,
                         'sha': commit['sha']}, conflict=True)
        return commit['sha']


class WorkerStore:
    """Изменения страниц повторяются поверх свежего состояния, сохраняя правки API."""
    def __init__(self, store):
        self.store = store
        self.pending_items = []
        self.state = None
        self.sha = None

    def ensure_loaded(self):
        if self.state is None:
            self.state, self.sha = self.store.load()

    def can_request_meta(self):
        self.ensure_loaded()
        remaining = getattr(self.store, 'remaining', None)
        return remaining is None or remaining >= 15

    def mutate(self, operation):
        for attempt in range(3):
            self.ensure_loaded()
            state, sha = deepcopy(self.state), self.sha
            previous = deepcopy(state)
            operation(state)
            if previous == state:
                return sha
            try:
                self.sha = self.store.save(state, sha)
                self.state = state
                return self.sha
            except Conflict:
                if attempt == 2:
                    raise
                self.state, self.sha = self.store.load()

    def authors(self):
        self.ensure_loaded()
        state = self.state
        rows = [deepcopy(row) for row in state['authors'] if row['active']]
        until = _stamp(state.get('meta_cooldown_until'))
        if until and until > datetime.now(timezone.utc):
            for row in rows:
                row.update(status='meta_paused', next_check_at=until.isoformat(),
                           error=state.get('meta_cooldown_reason', 'meta_rate_limit'))
        return rows

    def active(self, username):
        return any(row['username'] == username for row in self.authors())

    def save(self, items):
        # Карточки и курсор страницы отправляются одним атомарным коммитом update.
        self.pending_items = deepcopy(items)

    def update(self, username, *, pause_until=None, pause_reason=None, **values):
        items = deepcopy(self.pending_items)
        def operation(state):
            row = next((row for row in state['authors'] if row['username'] == username and row['active']), None)
            if row is None:
                return
            row.update(deepcopy(values))
            existing = {item['id']: item for item in state['items']}
            existing.update({item['id']: item for item in items})
            state['items'] = list(existing.values())
            if pause_until:
                self.apply_pause(state, pause_until, pause_reason)
        self.mutate(operation)
        self.pending_items = []

    @staticmethod
    def apply_pause(state, until, reason):
        state['meta_cooldown_until'] = until
        state['meta_cooldown_reason'] = reason
        for row in state['authors']:
            if row['active']:
                row.update(status='meta_paused', next_check_at=until, error=reason)

    def pause(self, until, reason, *, username=None, last_attempt_at=None):
        def operation(state):
            self.apply_pause(state, until, reason)
            for row in state['authors']:
                if row['username'] == username:
                    row['last_attempt_at'] = last_attempt_at
        self.mutate(operation)

    def prune(self, start):
        def operation(state):
            state['items'] = [item for item in state['items'] if _stamp(item['timestamp']) >= start]
        self.mutate(operation)
