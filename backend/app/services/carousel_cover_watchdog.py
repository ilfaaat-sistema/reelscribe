"""Независимый сигнал существующей очереди: фиксированные repo/event, без модельных POST."""
from __future__ import annotations

import httpx
from datetime import datetime, timezone

from app.core.config import settings
from app.services.carousel_cover_background_store import CarouselCoverBackgroundStore, source_refs
from app.services.carousel_cover_store import CoverStore
from app.services.carousel_cover_watchdog_store import WatchdogStore

REPOSITORY = 'ilfaaat-sistema/reelscribe'
EVENT = 'carousel-cover-background-run'
WORKFLOW = 'carousel-cover-background.yml'
VARIABLE = 'CAROUSEL_COVER_BACKGROUND_ENABLED'
BUDGET_FIELDS = ('status', 'mode', 'daily_limit', 'daily_attempts', 'attempts', 'active_slots', 'available_slots', 'reset_at')
SUMMARY_FIELDS = ('pending', 'running', 'unknown', 'terminal', 'remaining')
REASONS = {'disabled', 'controller_unavailable', 'dispatch_gate', 'repository_disabled', 'active_run',
           'quota', 'busy', 'paused', 'auth_failed', 'unknown_only', 'empty', 'dispatch_sent',
           'github_unavailable', 'watchdog_error', 'source_changed', 'work_pending'}


def safe_summary(state):
    values = {name: 0 for name in SUMMARY_FIELDS}
    for item in state['items']:
        status = item['status']
        if status == 'pending':
            values['pending'] += 1
        elif status in {'running', 'inflight'}:
            values['running'] += 1
        elif status == 'unknown':
            values['unknown'] += 1
        else:
            values['terminal'] += 1
    values['remaining'] = values['pending'] + values['running'] + values['unknown']
    return values


def public_status(control, budget=None):
    # Никаких source refs, ID, URL, текстов, claim/gate token в публичном контракте.
    summary = control.get('summary') or {}
    reason = control.get('reason')
    return {
        'enabled': control.get('enabled') is True,
        'mode': 'continuous',
        'reason': reason if reason in REASONS else 'disabled',
        'heartbeat': control.get('heartbeat'),
        'last_dispatch_at': control.get('last_dispatch_at'),
        'not_before': control.get('not_before'),
        'summary': {key: summary[key] for key in SUMMARY_FIELDS
                    if type(summary.get(key)) is int and summary[key] >= 0},
        'budget': {key: budget[key] for key in BUDGET_FIELDS if budget and key in budget},
    }


class GitHubWatchdog:
    def __init__(self, client, token):
        self.client, self.token = client, token
        self.queue = CarouselCoverBackgroundStore(client, token, request_timeout=5)

    def repository_enabled(self):
        value = self.queue.request('GET', 'actions/variables/' + VARIABLE)
        return value.get('value') == 'true'

    def active_run(self):
        for status in ('queued', 'in_progress'):
            value = self.queue.request('GET', f'actions/workflows/{WORKFLOW}/runs',
                params={'status': status, 'per_page': 1, 'branch': 'main'})
            if type(value.get('total_count')) is not int or not isinstance(value.get('workflow_runs'), list):
                raise ValueError('Некорректный ответ GitHub runs')
            if value['total_count'] > 0:
                return True
        return False

    def refs(self):
        return source_refs(self.queue)

    def load(self):
        return self.queue.load()[0]

    def dispatch(self):
        # repository_dispatch имеет фиксированный тип; клиент ничего не выбирает.
        response = self.client.post(f'https://api.github.com/repos/{REPOSITORY}/dispatches',
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'},
            json={'event_type': EVENT}, timeout=15)
        if response.status_code != 204:
            raise RuntimeError('GitHub dispatch недоступен')


class Watchdog:
    def __init__(self, store=None, cover=None, github=None):
        self.store = store or WatchdogStore()
        self.cover = cover or CoverStore()
        self.github = github

    def status(self):
        try:
            control = self.store.status()
        except Exception:  # noqa: BLE001 — pre-DDL выключен, без утечки SQL/заголовков.
            return public_status({'reason': 'controller_unavailable'})
        try:
            budget = self.cover.budget()
        except Exception:  # noqa: BLE001
            budget = None
        return public_status(control, budget)

    def tick(self):
        try:
            control = self.store.status()
        except Exception:  # noqa: BLE001
            return {'reason': 'controller_unavailable', 'dispatched': False}
        if control.get('enabled') is not True or control.get('mode') != 'continuous':
            return {'reason': 'disabled', 'dispatched': False}
        # Gate блокирует также параллельные публичные tick и повтор после потерянного ответа.
        try:
            claim = self.store.claim()
        except Exception:  # noqa: BLE001
            return {'reason': 'controller_unavailable', 'dispatched': False}
        if claim.get('claimed') is not True:
            return {'reason': 'dispatch_gate', 'dispatched': False}
        token = claim['token']
        refs, summary = {}, {}
        reason = 'watchdog_error'
        dispatched = False
        try:
            self.store.reap()
            if not self.github.repository_enabled():
                reason = 'repository_disabled'
            elif self.github.active_run():
                reason = 'active_run'
            else:
                budget = self.cover.budget()
                if budget.get('status') != 'available':
                    reason = 'quota'
                elif budget.get('available_slots', 0) <= 0:
                    reason = 'busy'
                else:
                    refs = self.github.refs()
                    state = self.github.load()
                    summary = safe_summary(state)
                    changed = refs != state.get('source_refs', {})
                    # Auth и неизвестный HTTP исход требуют оператора, а не пустой цепочки.
                    if state.get('pause_reason') == 'auth_failed':
                        reason = 'auth_failed'
                    elif state.get('pause_until') and datetime.fromisoformat(state['pause_until'].replace('Z', '+00:00')) > datetime.now(timezone.utc):
                        reason = 'paused'
                    elif summary['pending'] or changed:
                        # Повторная проверка disabled непосредственно перед внешним сигналом.
                        if self.store.status().get('enabled') is not True:
                            reason = 'disabled'
                        else:
                            self.github.dispatch()
                            reason, dispatched = 'dispatch_sent', True
                    else:
                        reason = 'unknown_only' if summary['unknown'] or summary['running'] else 'empty'
        except Exception:  # noqa: BLE001 — не печатаем HTTP request/response или заголовки.
            reason = 'watchdog_error'
        try:
            self.store.finish(token, reason, refs, summary)
        except Exception:  # noqa: BLE001 — gate остаётся закрытым даже если save потерян.
            pass
        return {'reason': reason, 'dispatched': dispatched}


def tick():
    # Существующий серверный секрет не передаётся в cron, response или worker.
    with httpx.Client(follow_redirects=False, trust_env=False, timeout=15) as client:
        github = GitHubWatchdog(client, settings.github_dispatch_token)
        return Watchdog(github=github).tick()


def status():
    return Watchdog().status()
