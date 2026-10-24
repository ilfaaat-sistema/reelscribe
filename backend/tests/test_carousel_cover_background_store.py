"""Проверки GitHub CAS-хранилища без сети и учётных данных."""
import base64
import json

import httpx
import pytest
from app.services.carousel_cover_background_store import (
    BRANCH,
    FILES,
    MARKER,
    CarouselCoverBackgroundStore,
    Conflict,
    initial_state,
    validate_state,
)

SHA = "a" * 40
BLOB = "b" * 40


def multiline_base64(raw):
    encoded = base64.b64encode(raw).decode()
    return " \t" + encoded[:5] + "\r\n" + encoded[5:11] + "\f\v" + encoded[11:] + "\n "


def test_load_checks_exact_marker_vercel_and_file_set():
    state = initial_state()
    blobs = {
        "state.json": json.dumps(state).encode(),
        "vercel.json": b'{"git":{"deploymentEnabled":false}}',
        ".carousel-cover-background-data": MARKER.encode(),
    }
    sha_by_name = {name: (str(index + 1) * 40) for index, name in enumerate(sorted(FILES))}
    by_sha = {sha_by_name[name]: value for name, value in blobs.items()}

    def handler(request):
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [
                {"path": name, "type": "blob", "mode": "100644", "sha": sha_by_name[name]}
                for name in FILES
            ]})
        digest = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(by_sha[digest]).decode()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        loaded, sha = CarouselCoverBackgroundStore(client).load()
    assert loaded == state and sha == SHA


def test_save_uses_expected_parent_and_non_force_cas():
    state = initial_state()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/git/trees"):
            body = json.loads(request.content)
            names = {row["path"] for row in body["tree"]}
            assert names == FILES
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.url.path.endswith("/git/commits"):
            assert json.loads(request.content)["parents"] == [SHA]
            return httpx.Response(201, json={"sha": "e" * 40})
        assert request.method == "PATCH"
        assert request.url.path.endswith("/git/refs/heads/" + BRANCH)
        assert json.loads(request.content) == {"sha": "e" * 40, "force": False}
        return httpx.Response(422, json={})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(Conflict),
    ):
        CarouselCoverBackgroundStore(client, "dummy").save(state, SHA)
    assert len(requests) == 3


def test_github_rate_metadata_is_safe_and_available_to_worker():
    reset = "1792700000"

    def handler(request):
        return httpx.Response(404, headers={"x-ratelimit-remaining": "19", "x-ratelimit-reset": reset}, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = CarouselCoverBackgroundStore(client)
        assert store.load()[1] is None
    assert store.remaining == 19 and int(store.reset_at.timestamp()) == int(reset)


def test_state_is_strict_and_rejects_text_credentials_and_invalid_dates():
    state = initial_state()
    assert validate_state(state)
    for field in ("caption", "translation", "token", "authorization"):
        bad = {**state, field: "forbidden"}
        with pytest.raises(ValueError):
            validate_state(bad)
    for field in ("updated_at", "pause_until"):
        with pytest.raises(ValueError):
            validate_state({**state, field: "2026-10-23T12:00:00"})
    with pytest.raises(ValueError):
        validate_state({**state, "pause_reason": "github_pat_" + "a" * 30})


def test_marker_or_extra_code_refuses_existing_branch():
    def handler(request):
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        return httpx.Response(200, json={"tree": [{"path": "app.py", "type": "blob", "mode": "100644", "sha": BLOB}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        CarouselCoverBackgroundStore(client).load()


def test_initial_save_creates_orphan_data_branch_with_no_application_tree():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("git/trees"):
            assert "base_tree" not in body
            assert {row["path"] for row in body["tree"]} == FILES
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.url.path.endswith("git/commits"):
            assert body["parents"] == []
            return httpx.Response(201, json={"sha": "e" * 40})
        assert body == {"ref": "refs/heads/" + BRANCH, "sha": "e" * 40}
        return httpx.Response(201, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert CarouselCoverBackgroundStore(client, "dummy").save(initial_state(), None) == "e" * 40
    assert len(calls) == 3


def test_state_size_limit_and_duplicate_ids_are_rejected(monkeypatch):
    from app.services import carousel_cover_background_store as store
    from app.workers.carousel_cover_background import merge_inventory
    from datetime import datetime, timezone
    state = initial_state()
    merge_inventory(state, [{"id": "1", "periods": ["14d"]}], "existing", datetime.now(timezone.utc))
    state["items"].append(dict(state["items"][0]))
    with pytest.raises(ValueError):
        validate_state(state)
    state["items"].pop()
    monkeypatch.setattr(store, "MAX_BYTES", 100)
    with pytest.raises(ValueError, match="размер"):
        validate_state(state)


def test_v1_upgrade_preserves_unknown_history_and_never_treats_posts_as_paid():
    from app.services.carousel_cover_background_store import upgrade_state
    from app.workers.carousel_cover_background import merge_inventory
    from datetime import datetime, timezone
    state = initial_state()
    state['version'] = 1
    state.pop('source_refs')
    state.pop('recovery_events')
    for key in ('http_requests', 'attempts_reserved', 'cache_hits', 'busy_rejections'):
        state['counters'].pop(key)
    state['counters']['posts'] = 904
    now = datetime.now(timezone.utc)
    merge_inventory(state, [{'id': '1', 'periods': ['14d']}], 'continuous', now)
    state['items'][0].update(status='unknown', first_inflight_at=now.isoformat(), safe_error_code='post_outcome_unknown')
    upgraded = upgrade_state(state)
    assert upgraded['version'] == 2 and upgraded['items'] == state['items']
    assert upgraded['inventory'] == state['inventory']
    assert upgraded['counters']['posts'] == upgraded['counters']['http_requests'] == 904
    assert upgraded['counters']['attempts_reserved'] == 0
    assert state['version'] == 1  # Исходник не изменён.
    validate_state(upgraded)


def test_operator_reconciliation_requires_exact_manifest_and_cas_preserves_event():
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    from app.services.carousel_cover_background_store import reconcile_unknown
    from app.workers.carousel_cover_background import merge_inventory
    now = datetime.now(timezone.utc)
    state = initial_state()
    merge_inventory(state, [{'id': '1', 'periods': ['14d']}], 'continuous', now)
    first = (now - timedelta(seconds=601)).isoformat()
    state['items'][0].update(status='unknown', first_inflight_at=first)
    class Store:
        saved = None
        def load(self):
            return deepcopy(state), SHA
        def save(self, value, expected):
            assert expected == SHA
            validate_state(value)
            self.saved = value
            return BLOB
    store = Store()
    manifest = {'expected_sha': SHA, 'items': [{'id': '1', 'expected_updated_at': now.isoformat(),
        'expected_first_inflight_at': first, 'db_claim_absent': True, 'db_all_versions_checked': True, 'old_run_finished': True}]}
    with pytest.raises(Conflict):
        reconcile_unknown(store, {**manifest, 'expected_sha': BLOB}, now=now)
    bad = deepcopy(manifest)
    bad['items'][0]['old_run_finished'] = False
    with pytest.raises(ValueError):
        reconcile_unknown(store, bad, now=now)
    assert store.saved is None
    assert reconcile_unknown(store, manifest, now=now) == BLOB
    assert store.saved['items'][0]['status'] == 'pending'
    assert store.saved['recovery_events'][0]['first_inflight_at'] == first
    assert store.saved['counters'] == state['counters']


def test_source_refs_read_only_tree_metadata_without_source_contents():
    from app.services.carousel_cover_background_store import source_refs, SOURCES
    calls = []
    def handler(request):
        calls.append(request)
        assert '/git/trees/data/' in request.url.path
        assert request.method == 'GET'
        branch = request.url.path.split('/git/trees/', 1)[1]
        path = next(path for expected, path in SOURCES.values() if expected == branch)
        return httpx.Response(200, json={'tree': [{'path': path, 'type': 'blob', 'sha': SHA}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert source_refs(CarouselCoverBackgroundStore(client)) == {name: SHA for name in SOURCES}
    assert len(calls) == 3


@pytest.mark.parametrize(('action', 'proof', 'old_status', 'target'), [
    ('db_rearmed', {'db_rearmed': True}, 'review', 'pending'),
    ('resolved_without_model', {'resolved_without_model': True, 'observed_status': 'not_needed'}, 'review', 'not_needed'),
    ('resolved_without_model', {'resolved_without_model': True, 'observed_status': 'no_text'}, 'pending', 'no_text'),
])
def test_review_operator_manifest_preserves_audit_and_never_posts(action, proof, old_status, target):
    from copy import deepcopy
    from datetime import datetime, timezone
    from app.services.carousel_cover_background_store import reconcile_queue
    from app.workers.carousel_cover_background import merge_inventory
    now = datetime.now(timezone.utc)
    state = initial_state()
    merge_inventory(state, [{'id': '1', 'periods': ['14d']}], 'continuous', now)
    state['items'][0]['status'] = old_status
    class Store:
        saved = None
        def load(self):
            return deepcopy(state), SHA
        def save(self, value, expected):
            assert expected == SHA
            validate_state(value)
            self.saved = value
            return BLOB
    store = Store()
    row = {'id': '1', 'expected_status': old_status, 'expected_updated_at': now.isoformat(),
           'expected_first_inflight_at': None, 'action': action, **proof}
    assert reconcile_queue(store, {'expected_sha': SHA, 'items': [row]}, now=now) == BLOB
    assert store.saved['items'][0]['status'] == target
    assert store.saved['recovery_events'][0]['previous_status'] == old_status
    assert store.saved['counters'] == state['counters']
    for status in ('ready', 'not_needed', 'no_text', 'unknown'):
        state['items'][0]['status'] = status
        bad = {**row, 'expected_status': status}
        with pytest.raises(ValueError):
            reconcile_queue(store, {'expected_sha': SHA, 'items': [bad]}, now=now)


def test_unknown_manifest_rejects_incomplete_db_check_or_age_at_600s():
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    from app.services.carousel_cover_background_store import reconcile_queue
    from app.workers.carousel_cover_background import merge_inventory
    now = datetime.now(timezone.utc)
    state = initial_state()
    merge_inventory(state, [{'id': '1', 'periods': ['14d']}], 'continuous', now)
    state['items'][0].update(status='unknown', first_inflight_at=(now - timedelta(seconds=600)).isoformat())
    class Store:
        def load(self):
            return deepcopy(state), SHA
        def save(self, *args):
            pytest.fail('Неподтверждённый unknown изменён')
    row = {'id': '1', 'expected_status': 'unknown', 'expected_updated_at': now.isoformat(),
           'expected_first_inflight_at': state['items'][0]['first_inflight_at'], 'action': 'verified_no_attempt',
           'db_all_versions_checked': True, 'db_claim_absent': True, 'old_run_finished': True}
    with pytest.raises(ValueError):
        reconcile_queue(Store(), {'expected_sha': SHA, 'items': [row]}, now=now)
    state['items'][0]['first_inflight_at'] = (now - timedelta(seconds=601)).isoformat()
    row['expected_first_inflight_at'] = state['items'][0]['first_inflight_at']
    row['db_all_versions_checked'] = False
    with pytest.raises(ValueError):
        reconcile_queue(Store(), {'expected_sha': SHA, 'items': [row]}, now=now)
