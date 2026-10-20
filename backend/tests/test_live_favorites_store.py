import base64
import json
from copy import deepcopy

import httpx
import pytest
from app.radar.live_favorites_store import (
    MARKER,
    Conflict,
    LiveFavoritesStore,
    WorkerStore,
    initial_state,
    validate_state,
)


def test_initial_four_and_validation():
    state = initial_state()
    assert len(state['authors']) == 4
    assert validate_state(state)
    state['authors'].append(deepcopy(state['authors'][0]))
    with pytest.raises(ValueError):
        validate_state(state)


@pytest.mark.parametrize('secret', ['Bearer ' + 'secret' * 12, 'github_pat_' + 'a' * 40,
                                    'EAA' + 'a' * 60])
def test_secret_rejected(secret):
    state = initial_state()
    state['authors'][0]['error'] = secret
    with pytest.raises(ValueError):
        validate_state(state)


def test_missing_branch_public_read_and_initial_cas():
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == 'GET':
            assert 'authorization' not in request.headers
            return httpx.Response(404)
        if request.url.path.endswith('/git/trees'):
            tree = json.loads(request.content)['tree']
            assert {row['path'] for row in tree} == {'state.json', 'vercel.json', '.live-carousels-data'}
            assert json.loads(tree[1]['content'])['git']['deploymentEnabled'] is False
            return httpx.Response(201, json={'sha': 'tree'})
        if request.url.path.endswith('/git/commits'):
            assert json.loads(request.content)['parents'] == []
            return httpx.Response(201, json={'sha': 'new'})
        return httpx.Response(201, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = LiveFavoritesStore(client)
        state, sha = store.load()
        assert sha is None
        with pytest.raises(ValueError):
            store.save(state, sha)
        store.token = 'dummy'
        assert store.save(state, sha) == 'new'


def test_feed_read_uses_one_contents_request():
    state = initial_state()
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'type': 'file', 'encoding': 'base64',
            'content': base64.b64encode(json.dumps(state).encode()).decode()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert LiveFavoritesStore(client, 'dummy').read_state() == state
    assert len(calls) == 1 and '/contents/state.json?ref=' in calls[0]


def test_load_pinned_commit_and_conflict_without_force():
    state = initial_state()
    def handler(request):
        path = request.url.path
        if request.method == 'PATCH':
            assert json.loads(request.content) == {'sha': 'new', 'force': False}
            return httpx.Response(422, json={})
        if request.method == 'POST':
            if path.endswith('/git/commits'):
                assert json.loads(request.content)['parents'] == ['old']
                return httpx.Response(201, json={'sha': 'new'})
            return httpx.Response(201, json={'sha': 'tree'})
        if '/git/ref/' in path:
            return httpx.Response(200, json={'object': {'sha': 'old'}})
        if '/git/commits/' in path:
            assert path.endswith('/old')
            return httpx.Response(200, json={'tree': {'sha': 'tree'}})
        if '/git/trees/' in path:
            return httpx.Response(200, json={'tree': [
                {'path': name, 'type': 'blob', 'mode': '100644', 'sha': name}
                for name in ('state.json', '.live-carousels-data', 'vercel.json')]})
        return httpx.Response(200, json={'encoding': 'base64', 'content':
            base64.b64encode((MARKER if path.endswith('/.live-carousels-data') else
                json.dumps({'git': {'deploymentEnabled': False}}) if path.endswith('/vercel.json') else
                json.dumps(state)).encode()).decode()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = LiveFavoritesStore(client, 'dummy')
        loaded, sha = store.load()
        assert loaded == state and sha == 'old'
        with pytest.raises(Conflict):
            store.save(loaded, sha)


class MemoryGit:
    def __init__(self):
        self.state = initial_state()
        self.conflicts = 0
        self.writes = 0

    def load(self):
        return deepcopy(self.state), 'old'

    def save(self, state, sha):
        self.writes += 1
        if self.conflicts:
            self.conflicts -= 1
            self.state['authors'].append(dict(self.state['authors'][0], username='new_author'))
            self.state['authors'][0]['active'] = False
            raise Conflict()
        self.state = deepcopy(state)
        return 'new'


def test_worker_merges_concurrent_api_add_and_remove_atomically():
    git = MemoryGit()
    worker = WorkerStore(git)
    worker.save([{'id': '1', 'username': 'artemiy.miller', 'timestamp': '2026-10-20T00:00:00+00:00'}])
    assert git.writes == 0
    git.conflicts = 1
    worker.update('artemiy.miller', checkpoint={'after': 'next'})
    assert git.writes == 2
    assert git.state['authors'][0]['active'] is False
    assert git.state['authors'][-1]['username'] == 'new_author'
    assert git.state['authors'][1]['checkpoint'] == {'after': 'next'}
    assert git.state['items'][0]['id'] == '1'


def test_worker_conflict_limit_and_deleted_author_not_revived():
    git = MemoryGit()
    worker = WorkerStore(git)
    git.conflicts = 3
    with pytest.raises(Conflict):
        worker.update('artemiy.miller', status='updated')
    assert git.writes == 3
    git.state['authors'][0]['active'] = False
    worker.update('alex_magnier', status='updated')
    assert git.state['authors'][0]['status'] == 'pending'


def test_cooldown_survives_removing_all_previous_authors():
    from datetime import datetime, timedelta, timezone
    git = MemoryGit()
    worker = WorkerStore(git)
    until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    worker.pause(until, 'meta_usage_85')
    for row in git.state['authors']:
        row['active'] = False
    git.state['authors'].append(dict(initial_state()['authors'][0], username='new_author'))
    rows = WorkerStore(git).authors()
    assert len(rows) == 1
    assert rows[0]['status'] == 'meta_paused'
    assert rows[0]['next_check_at'] == until


@pytest.mark.parametrize('wrong', ['missing', 'marker', 'vercel', 'extra'])
def test_refuse_foreign_branch(wrong):
    def handler(request):
        path = request.url.path
        if '/git/ref/' in path:
            return httpx.Response(200, json={'object': {'sha': 'commit'}})
        if '/git/commits/' in path:
            return httpx.Response(200, json={'tree': {'sha': 'tree'}})
        if '/git/trees/' in path:
            names = [] if wrong == 'missing' else ['.live-carousels-data', 'vercel.json', 'state.json']
            if wrong == 'extra':
                names.append('unrelated.json')
            return httpx.Response(200, json={'tree': [{'path': name, 'type': 'blob', 'mode': '100644', 'sha': name} for name in names]})
        text = ('чужие данные' if wrong == 'marker' else MARKER) if path.endswith('/.live-carousels-data') else '{}'
        return httpx.Response(200, json={'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        LiveFavoritesStore(client).load()


def test_prune_without_expired_items_does_not_write():
    from datetime import datetime, timezone
    git = MemoryGit()
    WorkerStore(git).prune(datetime.now(timezone.utc))
    assert git.writes == 0


def test_worker_cache_one_load_and_one_commit_per_page():
    git = MemoryGit()
    loads = []
    original = git.load
    def load():
        loads.append(1)
        return original()
    git.load = load
    worker = WorkerStore(git)
    worker.authors()
    worker.active('alex_magnier')
    worker.prune(__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
    worker.save([])
    worker.update('alex_magnier', status='updated')
    worker.active('alex_magnier')
    worker.update('artemiy.miller', status='updated')
    assert len(loads) == 1
    assert git.writes == 2


def test_contents_large_file_uses_object_media_type_and_blob_fallback():
    state = initial_state()
    state['padding'] = 'x' * (1024 * 1024 + 10)
    calls = []
    def handler(request):
        calls.append(request)
        if '/contents/' in request.url.path:
            assert request.headers['accept'] == 'application/vnd.github.object+json'
            return httpx.Response(200, json={'type': 'file', 'encoding': 'none', 'sha': 'blob'})
        assert request.url.path.endswith('/git/blobs/blob')
        return httpx.Response(200, json={'encoding': 'base64', 'content':
            base64.b64encode(json.dumps(state).encode()).decode()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert LiveFavoritesStore(client).read_state() == state
    assert len(calls) == 2


def test_github_remaining_header_is_tracked_for_reserve():
    def handler(request):
        return httpx.Response(404, headers={'x-ratelimit-remaining': '14'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = LiveFavoritesStore(client, 'dummy')
        worker = WorkerStore(store)
        assert worker.can_request_meta() is False
        assert store.remaining == 14


def test_http_cache_saves_three_github_requests_per_page():
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == 'GET':
            return httpx.Response(404, headers={'x-ratelimit-remaining': '200'})
        if request.url.path.endswith('/git/trees'):
            return httpx.Response(201, json={'sha': 'tree'})
        if request.url.path.endswith('/git/commits'):
            return httpx.Response(201, json={'sha': 'new'})
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = WorkerStore(LiveFavoritesStore(client, 'dummy'))
        worker.authors()
        for name in ('alex_magnier', 'artemiy.miller'):
            assert worker.active(name)
            assert worker.can_request_meta()
            worker.save([])
            worker.update(name, status='updated')
        assert len([r for r in calls if r.method == 'GET']) == 1
        assert len([r for r in calls if r.method != 'GET']) == 6


def test_success_page_and_usage_pause_commit_together():
    git = MemoryGit()
    worker = WorkerStore(git)
    worker.update('alex_magnier', checkpoint={'after': 'next'},
                  pause_until='2099-01-01T00:00:00+00:00', pause_reason='meta_usage_85')
    assert git.writes == 1
    assert git.state['authors'][0]['checkpoint'] == {'after': 'next'}
    assert git.state['authors'][0]['status'] == 'meta_paused'
