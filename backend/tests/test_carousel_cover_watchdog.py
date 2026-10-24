"""Watchdog: никаких реальных dispatch, credentials, моделей или сети."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.carousel_cover_watchdog import router
from app.services import carousel_cover_watchdog as service
from app.services.carousel_cover_background_store import initial_state
from app.workers.carousel_cover_background import merge_inventory

NOW = datetime.now(timezone.utc)
REFS = {name: 'a' * 40 for name in ('archive7d', 'archive14d', 'live')}


class Control:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.claimed = False
        self.records = []
        self.reaps = 0
        self.lock = Lock()

    def status(self):
        return {'enabled': self.enabled, 'mode': 'continuous', 'reason': 'empty', 'last_seen_source_refs': REFS,
                'summary': {'pending': 3}, 'gate_token': 'private-gate'}

    def claim(self):
        with self.lock:
            if self.claimed:
                return {'claimed': False}
            self.claimed = True
            return {'claimed': True, 'token': 'private-gate'}

    def finish(self, token, reason, refs, summary):
        assert token == 'private-gate'
        self.records.append((reason, refs, summary))

    def reap(self):
        self.reaps += 1


class Cover:
    def __init__(self, status='available', slots=2):
        self.value = {'status': status, 'available_slots': slots, 'daily_attempts': 21,
                      'active_slots': 2 - slots, 'attempts': 45, 'daily_limit': 500, 'mode': 'live'}

    def budget(self):
        return self.value


class GitHub:
    def __init__(self, count=1, *, active=False, changed=False, enabled=True):
        self.state = initial_state('continuous')
        merge_inventory(self.state, [{'id': str(i + 1), 'periods': ['30d']} for i in range(count)], 'continuous', NOW)
        self.state['source_refs'] = {} if changed else REFS.copy()
        self.active, self.enabled = active, enabled
        self.signals = 0
        self.loads = 0
        self.ref_reads = 0

    def repository_enabled(self):
        return self.enabled

    def active_run(self):
        return self.active

    def refs(self):
        self.ref_reads += 1
        return REFS.copy()

    def load(self):
        self.loads += 1
        return deepcopy(self.state)

    def dispatch(self):
        self.signals += 1


def watchdog(control=None, cover=None, github=None):
    return service.Watchdog(control or Control(), cover or Cover(), github or GitHub())


def test_disabled_is_noop_without_gate_github_reap_or_budget():
    control, github = Control(False), GitHub()
    assert watchdog(control=control, github=github).tick() == {'reason': 'disabled', 'dispatched': False}
    assert not control.claimed and not control.reaps and not github.loads and not github.signals


@pytest.mark.parametrize('run_status', ['queued', 'in_progress'])
def test_queued_or_active_run_refreshes_summary_without_source_reads_or_dispatch(run_status):
    control, snapshot = Control(), GitHub(count=6)
    for item, status in zip(snapshot.state['items'], ['pending', 'pending', 'running', 'unknown', 'ready', 'not_needed']):
        item['status'] = status
    calls, loads = [], []
    def handler(request):
        calls.append(request)
        if '/actions/variables/' in request.url.path:
            return httpx.Response(200, json={'value': 'true'})
        assert request.method == 'GET' and request.url.path.endswith('/runs')
        active = request.url.params['status'] == run_status
        return httpx.Response(200, json={'total_count': int(active), 'workflow_runs': [{'id': 1}] if active else []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        github = service.GitHubWatchdog(client, 'dummy-test-token')
        github.load = lambda: loads.append(True) or deepcopy(snapshot.state)
        assert watchdog(control=control, github=github).tick() == {'reason': 'active_run', 'dispatched': False}
    assert loads == [True] and control.reaps == 1
    assert control.records == [('active_run', {}, {'pending': 2, 'running': 1, 'unknown': 1, 'terminal': 2, 'remaining': 4})]
    assert all(request.method == 'GET' for request in calls)
    assert not any('/git/trees/' in request.url.path for request in calls)


@pytest.mark.parametrize(('status', 'slots', 'reason'), [('limit_reached', 2, 'quota'), ('available', 0, 'busy')])
def test_quota_and_busy_do_not_load_sources_or_dispatch(status, slots, reason):
    github = GitHub()
    assert watchdog(cover=Cover(status, slots), github=github).tick()['reason'] == reason
    assert not github.loads and not github.ref_reads and not github.signals


def test_repository_variable_remains_an_independent_safeguard():
    github = GitHub(enabled=False)
    assert watchdog(github=github).tick()['reason'] == 'repository_disabled'
    assert not github.ref_reads and not github.signals


def test_existing_pending_dispatches_once_and_public_duplicate_tick_is_atomic():
    control, github = Control(), GitHub()
    controller = watchdog(control=control, github=github)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: controller.tick(), range(8)))
    assert sum(row['dispatched'] for row in results) == 1
    assert github.signals == 1 and control.reaps == 1
    assert sum(row['reason'] == 'dispatch_gate' for row in results) == 7
    assert control.records[0][2]['pending'] == 1


@pytest.mark.parametrize('status', ['ready', 'not_needed', 'unknown', 'running'])
def test_no_progress_terminal_or_unknown_only_never_dispatches(status):
    github = GitHub()
    github.state['items'][0]['status'] = status
    result = watchdog(github=github).tick()
    assert result['reason'] in {'empty', 'unknown_only'} and not github.signals


def test_changed_source_sha_dispatches_once_without_catalog_download_in_tick():
    github = GitHub(count=0, changed=True)
    assert watchdog(github=github).tick()['dispatched']
    assert github.ref_reads == 1 and github.loads == 1
    # Сам tick не вызывает inventory/POST translation; worker сверит изменённый каталог.


@pytest.mark.parametrize('reason', ['provider_rate_limit', 'three_errors', 'busy_backpressure'])
def test_future_short_pause_defers_dispatch_and_later_tick_can_resume(reason):
    github = GitHub()
    github.state.update(pause_reason=reason, pause_until=(NOW + timedelta(hours=1)).isoformat())
    assert watchdog(github=github).tick()['reason'] == 'paused'
    assert not github.signals
    github.state['pause_until'] = (NOW - timedelta(hours=1)).isoformat()
    assert watchdog(github=github).tick()['dispatched']


def test_transport_failure_never_releases_dispatch_gate_or_leaks_error():
    github, control = GitHub(), Control()
    def uncertain():
        github.signals += 1
        raise httpx.ReadTimeout('Authorization: SECRET')
    github.dispatch = uncertain
    controller = watchdog(control=control, github=github)
    result = controller.tick()
    assert result == {'reason': 'watchdog_error', 'dispatched': False}
    assert 'SECRET' not in str(result) and control.claimed
    assert controller.tick()['reason'] == 'dispatch_gate'
    assert github.signals == 1


def test_status_contains_only_safe_aggregates_and_budget():
    result = watchdog().status()
    assert result['enabled'] and result['budget']['daily_attempts'] == 21
    assert result['summary'] == {'pending': 3}
    assert not {'gate_token', 'last_seen_source_refs', 'items'} & result.keys()
    assert 'private-gate' not in str(result) and 'a' * 40 not in str(result)


def test_pre_ddl_controller_failure_is_safe_disabled_status():
    control = Control()
    def missing():
        raise RuntimeError('private database message')
    control.status = missing
    result = watchdog(control=control).status()
    assert not result['enabled'] and result['reason'] == 'controller_unavailable'
    assert watchdog(control=control).tick()['reason'] == 'controller_unavailable'
    assert 'private database' not in str(result)


@pytest.mark.parametrize('run_status', ['queued', 'in_progress'])
def test_real_github_adapter_checks_workflow_and_fixed_dispatch(run_status):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == 'api.github.com'
        assert '/repos/ilfaaat-sistema/reelscribe/' in request.url.path
        if request.url.path.endswith('/runs'):
            assert request.url.params['branch'] == 'main'
            active = request.url.params['status'] == run_status
            return httpx.Response(200, json={'total_count': int(active), 'workflow_runs': [{'id': 1}] if active else []})
        assert request.method == 'POST' and request.url.path.endswith('/dispatches')
        assert __import__('json').loads(request.content) == {'event_type': 'carousel-cover-background-run'}
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        github = service.GitHubWatchdog(client, 'dummy-test-token')
        assert github.active_run()
        github.dispatch()
    assert len(calls) == (2 if run_status == 'queued' else 3)


def test_tick_api_rejects_parameters_and_accepts_empty_body_only(monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'tick', lambda: calls.append(True) or {'dispatched': False, 'reason': 'disabled'})
    monkeypatch.setattr(service, 'status', lambda: {'enabled': False})
    app = FastAPI()
    app.include_router(router, prefix='/api')
    with TestClient(app) as client:
        for body in ({'ids': ['1']}, {'budget': 2500}, {'model': 'x'}, [], 'text', None, True):
            response = client.post('/api/radar/carousel-cover-background/tick', content=__import__('json').dumps(body))
            assert response.status_code == 422
        assert client.post('/api/radar/carousel-cover-background/tick?ids=1').status_code == 422
        assert not calls
        assert client.post('/api/radar/carousel-cover-background/tick', json={}).status_code == 200
        assert client.post('/api/radar/carousel-cover-background/tick').status_code == 200
        assert len(calls) == 2
        response = client.get('/api/radar/carousel-cover-background/status')
        assert response.json() == {'enabled': False} and response.headers['cache-control'] == 'no-store'


def test_workflow_explicit_continuation_event_timeout_and_permissions():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / '.github/workflows/carousel-cover-background.yml').read_text()
    assert 'actions: write' in text and 'timeout-minutes: 90' in text
    assert 'repository_dispatch:' in text and 'types: [carousel-cover-background-run]' in text
    assert 'cancel-in-progress: false' in text
    assert "if: steps.queue.outputs.needs_continuation == 'true'" in text
    assert 'gh workflow run carousel-cover-background.yml --repo ilfaaat-sistema/reelscribe --ref main' in text
