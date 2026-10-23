"""Локальная приёмка worker с fake HTTP/store; платных вызовов нет."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from app.services.carousel_caption_bulk_store import initial_state
from app.services.carousel_caption_rules import source_hash
from app.workers import carousel_caption_bulk as worker

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def row(identity="123", caption="An English caption", timestamp="2026-10-23T10:00:00+00:00"):
    return {"id": identity, "caption": caption, "timestamp": timestamp, "media_type": "CAROUSEL_ALBUM"}


def snapshots(*rows):
    values = list(rows) or [row()]
    return {
        "archive7d": {"provider": "meta_graph", "items": values},
        "archive14d": {"provider": "meta_graph", "items": values},
        "live": {"version": 1, "items": values},
    }


class MemoryStore:
    def __init__(self, data=None):
        self.state = initial_state()
        self.data = data or snapshots()
        self.refs = {name: {"commit": (str(index + 1) * 40), "blob": (str(index + 4) * 40)}
                     for index, name in enumerate(worker.SOURCES)}
        self.remaining = 5000
        self.reset_at = NOW + timedelta(hours=1)
        self.writes = []

    def load(self):
        return deepcopy(self.state), "a" * 40

    def save(self, state, expected):
        self.state = deepcopy(state)
        self.writes.append(deepcopy(state))
        return chr(98 + len(self.writes) % 20) * 40

    def source_ref(self, branch, path):
        name = next(name for name, spec in worker.SOURCES.items() if spec == (branch, path))
        value = self.refs[name]
        return value["commit"], value["blob"]

    def read_source_json(self, branch, path, expected_blob):
        name = next(name for name, spec in worker.SOURCES.items() if spec == (branch, path))
        assert expected_blob == self.refs[name]["blob"]
        return deepcopy(self.data[name])


class Api:
    def __init__(self, details=None, post=None):
        self.details = list(details or [])
        self.post_result = post
        self.gets = []
        self.posts = []

    def get(self, item):
        self.gets.append((item["id"], item["source_hash"]))
        value = self.details.pop(0) if self.details else {
            "source_hash": item["source_hash"], "state": "available",
            "attempt": {"status": "none", "started_at": None, "lease_until": None},
        }
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)

    def post(self, item, period=None):
        self.posts.append((item["id"], item["source_hash"], period or worker.preferred_period(item)))
        if isinstance(self.post_result, BaseException):
            raise self.post_result
        if callable(self.post_result):
            return self.post_result(item)
        return {"state": "ready", "source_hash": item["source_hash"]}


def execute(store, api, **kwargs):
    ticks = iter(range(10_000))
    return worker.run(store, api, now_fn=lambda: NOW, monotonic=lambda: next(ticks), **kwargs)


def test_inventory_deduplicates_periods_and_prefers_30d_without_persisting_text():
    inventory, counts = worker.build_inventory(snapshots(row()), NOW)
    assert len(inventory) == 1 and inventory[0]["periods"] == ["30d", "14d", "archive14d", "7d"]
    assert counts == {"eligible": 1}
    store, api = MemoryStore(), Api()
    status, values = execute(store, api, max_posts=1)
    assert status == "started" and values["ready"] == 1
    assert api.posts[0][2] == "30d"
    assert "caption" not in str(store.state) and "An English caption" not in str(store.state)


def test_ready_and_not_needed_are_completed_without_post():
    digest = source_hash("An English caption")
    for detail, expected in [
        ({"source_hash": digest, "state": "ready"}, "ready"),
        ({"source_hash": digest, "state": "not_needed"}, "not_needed"),
    ]:
        store, api = MemoryStore(), Api([detail])
        execute(store, api)
        assert store.state["items"][0]["status"] == expected
        assert not api.posts


def test_new_item_posts_once_with_automatic_contract_via_real_http_adapter():
    seen = []
    digest = source_hash("An English caption")
    item = {"id": "123", "source_hash": digest, "periods": ["archive14d"]}

    def handler(request):
        seen.append(request)
        body = __import__("json").loads(request.content)
        assert body == {"period": "archive14d", "source_hash": digest, "automatic": True}
        return httpx.Response(200, json={"caption_translation": {"state": "ready", "source_hash": digest}})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        assert worker.ProductionApi(client).post(item)["state"] == "ready"
    assert len(seen) == 1


def test_get_skips_live_404_and_posts_with_matching_archive_period():
    digest = source_hash("An English caption")
    item = {
        "id": "123", "source_hash": digest, "periods": ["30d", "archive14d"],
        "status": "inflight", "updated_at": NOW.isoformat(), "code": "checkpointed",
        "inflight_started_at": NOW.isoformat(),
    }
    seen = []

    def handler(request):
        period = request.url.params.get("period")
        seen.append((request.method, period))
        if request.method == "GET" and period == "30d":
            return httpx.Response(404, json={"code": "carousel_not_found"})
        if request.method == "GET" and period == "archive14d":
            return httpx.Response(200, json={"caption_translation": {
                "source_hash": digest, "state": "available",
                "attempt": {"status": "none", "started_at": None, "lease_until": None},
            }})
        assert request.method == "POST" and period is None
        assert __import__("json").loads(request.content)["period"] == "archive14d"
        return httpx.Response(200, json={"caption_translation": {"source_hash": digest, "state": "ready"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        action, posted = worker.process_one(worker.ProductionApi(client), item, initial_state(), NOW)
    assert (action, posted, item["status"]) == ("continue", True, "ready")
    assert seen == [("GET", "30d"), ("GET", "archive14d"), ("POST", None)]


def test_get_searches_other_period_after_wrong_hash():
    digest = source_hash("An English caption")
    item = {"id": "123", "source_hash": digest, "periods": ["30d", "archive14d"]}

    def handler(request):
        period = request.url.params["period"]
        return httpx.Response(200, json={"caption_translation": {
            "source_hash": source_hash("Changed") if period == "30d" else digest,
            "state": "available", "attempt": {"status": "none"},
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        detail = worker.ProductionApi(client).get(item)
    assert detail["source_hash"] == digest and detail["_period"] == "archive14d"


def test_get_marks_gone_only_after_all_periods_return_404():
    digest = source_hash("An English caption")
    item = {
        "id": "123", "source_hash": digest, "periods": ["30d", "archive14d", "7d"],
        "status": "inflight", "updated_at": NOW.isoformat(), "code": "checkpointed",
        "inflight_started_at": NOW.isoformat(),
    }
    calls = []

    def handler(request):
        calls.append(request.url.params["period"])
        return httpx.Response(404, json={"code": "carousel_not_found"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        action, posted = worker.process_one(worker.ProductionApi(client), item, initial_state(), NOW)
    assert calls == ["30d", "archive14d", "7d"]
    assert (action, posted, item["status"]) == ("continue", False, "gone")


def test_source_changed_never_posts_and_new_hash_is_discovered_later():
    store = MemoryStore()
    wrong = source_hash("Changed caption")
    api = Api([{"source_hash": wrong, "state": "available", "attempt": {"status": "none"}}])
    execute(store, api)
    assert store.state["items"][0]["status"] == "source_changed" and not api.posts
    store.data = snapshots(row(caption="Changed caption"))
    store.refs["live"] = {"commit": "8" * 40, "blob": "9" * 40}
    execute(store, Api(), max_posts=0)
    assert any(item["source_hash"] == wrong and item["status"] == "pending" for item in store.state["items"])


def test_same_refs_rebuild_live_windows_on_new_moscow_day():
    boundary = row(timestamp="2026-09-24T00:00:00+03:00")
    store = MemoryStore({
        "archive7d": {"provider": "meta_graph", "items": []},
        "archive14d": {"provider": "meta_graph", "items": []},
        "live": {"version": 1, "items": [boundary]},
    })
    state = initial_state()
    assert worker.discover(store, state, NOW) is True
    assert state["discovered_day"] == "2026-10-23" and state["inventory"]["eligible"] == 1
    assert worker.discover(store, state, NOW + timedelta(days=1)) is True
    assert state["discovered_day"] == "2026-10-24" and state["inventory"]["eligible"] == 0
    assert state["items"][0]["status"] == "gone"


def test_missing_source_becomes_gone_without_post():
    store, api = MemoryStore(), Api([worker.ApiFailure(404, "carousel_not_found")])
    execute(store, api)
    assert store.state["items"][0]["status"] == "gone"
    assert not api.posts


def test_atomic_previous_attempt_guard_becomes_review_without_retry():
    store, api = MemoryStore(), Api(post=worker.ApiFailure(409, "translation_review_required"))
    execute(store, api)
    assert store.state["items"][0]["status"] == "review"
    assert len(api.posts) == 1
    execute(store, Api())
    assert store.state["items"][0]["status"] == "review"


def test_old_api_without_attempt_fails_closed_and_only_pauses():
    store, api = MemoryStore(), Api([{"source_hash": source_hash("An English caption"), "state": "available"}])
    status, _ = execute(store, api)
    assert status == "paused" and not api.posts
    assert store.state["items"][0]["code"] == "old_api_contract"
    assert store.state["items"][0]["status"] == "inflight"
    retry = Api([{"source_hash": source_hash("An English caption"), "state": "available"}])
    execute(store, retry)
    assert len(retry.gets) == 1 and not retry.posts


@pytest.mark.parametrize("failure,code", [
    (worker.ApiFailure(429, "translation_rate_limited", 321), "translation_rate_limited"),
    (worker.ApiFailure(429, "translation_quota_exceeded"), "translation_quota_exceeded"),
])
def test_429_pauses_without_post_retry(failure, code):
    store, api = MemoryStore(), Api(post=failure)
    status, _ = execute(store, api)
    assert status == "paused" and len(api.posts) == 1
    assert store.state["pause_reason"] == code


def test_auth_503_reconciles_and_pauses_without_retry():
    digest = source_hash("An English caption")
    api = Api([
        {"source_hash": digest, "state": "available", "attempt": {"status": "none"}},
        {"source_hash": digest, "state": "available", "attempt": {"status": "none"}},
    ], post=worker.ApiFailure(503, "translation_unavailable"))
    store = MemoryStore()
    status, _ = execute(store, api)
    assert status == "paused" and len(api.posts) == 1 and len(api.gets) == 2
    assert store.state["items"][0]["status"] == "pending"


@pytest.mark.parametrize("status", [401, 403])
def test_post_auth_failure_pauses_after_first_item_without_reviewing_tail(status):
    rows = [row("1", "First English caption"), row("2", "Second English caption")]
    first_hash = source_hash(rows[0]["caption"])
    details = [
        {"source_hash": first_hash, "state": "available", "attempt": {"status": "none"}},
        {"source_hash": first_hash, "state": "available", "attempt": {"status": "none"}},
    ]
    store, api = MemoryStore(snapshots(*rows)), Api(details, post=worker.ApiFailure(status, "auth_failed"))
    result, _ = execute(store, api)
    items = {item["id"]: item for item in store.state["items"]}
    assert result == "paused" and len(api.posts) == 1
    assert store.state["pause_reason"] == "translation_auth_failed"
    assert items["1"]["status"] == "unknown"
    assert items["2"]["status"] == "pending"
    assert all(item["status"] != "review" for item in items.values())


def test_transport_unknown_is_checkpointed_and_never_retried():
    store, api = MemoryStore(), Api(post=httpx.ReadTimeout("unknown"))
    status, _ = execute(store, api)
    assert status == "paused" and len(api.posts) == 1
    assert store.state["items"][0]["status"] == "unknown"
    retry = Api()
    execute(store, retry)
    assert store.state["items"][0]["status"] == "unknown"
    assert len(retry.gets) == 1 and not retry.posts


def test_unparsed_502_pauses_as_unknown_without_retry():
    store, api = MemoryStore(), Api(post=worker.ApiFailure(502, "translation_unknown"))
    status, _ = execute(store, api)
    assert status == "paused" and len(api.posts) == 1
    assert store.state["items"][0]["status"] == "unknown"


def test_unknown_read_only_reconcile_can_recover_ready():
    store, api = MemoryStore(), Api(post=httpx.ReadTimeout("unknown"))
    execute(store, api)
    item = store.state["items"][0]
    retry = Api([{"source_hash": item["source_hash"], "state": "ready"}])
    execute(store, retry)
    assert store.state["items"][0]["status"] == "ready"
    assert not retry.posts


def test_crash_after_writeahead_recovers_only_unsent_after_360_seconds():
    store, api = MemoryStore(), Api(post=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        execute(store, api)
    checkpoint = store.state["items"][0]
    assert checkpoint["status"] == "inflight" and checkpoint["inflight_started_at"]
    later = NOW + timedelta(seconds=360)
    ticks = iter(range(100))
    detail = {"source_hash": checkpoint["source_hash"], "state": "available",
              "attempt": {"status": "none", "started_at": None, "lease_until": None}}
    worker.run(store, Api([detail]), now_fn=lambda: later, monotonic=lambda: next(ticks), max_posts=0)
    assert store.state["items"][0]["status"] == "pending"
    assert store.state["items"][0]["inflight_started_at"] is None


def test_expired_or_failed_attempt_never_posts_again():
    digest = source_hash("An English caption")
    for attempt_status, expected in [("expired_unknown", "unknown"), ("failed", "review"), ("quota", "review")]:
        store = MemoryStore()
        store.state["refs"] = deepcopy(store.refs)
        state_item = {
            "id": "123", "source_hash": digest, "periods": ["7d"], "status": "inflight",
            "updated_at": NOW.isoformat(), "code": "checkpointed", "inflight_started_at": NOW.isoformat(),
        }
        store.state["items"] = [state_item]
        store.state["inventory"]["eligible"] = 1
        api = Api([{"source_hash": digest, "state": "available", "attempt": {"status": attempt_status}}])
        execute(store, api, max_posts=0)
        assert store.state["items"][0]["status"] == expected and not api.posts


def test_known_running_attempt_stops_worker_from_using_second_slot():
    rows = [row("1", "First English caption"), row("2", "Second English caption")]
    store = MemoryStore(snapshots(*rows))
    first_hash = source_hash(rows[0]["caption"])
    api = Api([{"source_hash": first_hash, "state": "available", "attempt": {"status": "running"}}])
    status, _ = execute(store, api)
    assert status == "paused" and not api.posts
    assert store.state["pause_reason"] == "attempt_running"


def test_known_quota_attempt_pauses_until_next_moscow_day():
    digest = source_hash("An English caption")
    store = MemoryStore()
    api = Api([{"source_hash": digest, "state": "available", "attempt": {"status": "quota"}}])
    status, _ = execute(store, api)
    assert status == "paused" and not api.posts
    assert store.state["items"][0]["status"] == "review"
    assert store.state["pause_reason"] == "translation_quota_exceeded"


def test_three_closed_502_failures_pause_globally_and_each_posts_once():
    rows = [row(str(index), f"English caption {index}") for index in range(1, 4)]
    store = MemoryStore(snapshots(*rows))
    details = []
    for source in rows:
        digest = source_hash(source["caption"])
        details.extend([
            {"source_hash": digest, "state": "available", "attempt": {"status": "none"}},
            {"source_hash": digest, "state": "available", "attempt": {"status": "failed"}},
        ])
    api = Api(details, post=worker.ApiFailure(502, "translation_failed"))
    status, _ = execute(store, api)
    assert status == "paused" and len(api.posts) == 3
    assert store.state["pause_reason"] == "translation_failed_three"


def test_github_rate_guard_saves_pause_before_any_api_call():
    store, api = MemoryStore(), Api()
    store.remaining = 19
    status, _ = execute(store, api)
    assert status == "paused" and not api.gets and not api.posts
    assert store.state["pause_reason"] == "github_rate_limit"


def test_startup_validates_all_real_snapshot_rows_before_any_api_request():
    """Реальные сохранённые входы проверяются локально без публикации captions в git."""
    import hashlib
    import json
    from pathlib import Path

    from app.services.carousel_caption_bulk_store import validate_state

    worktree = Path(__file__).resolve().parents[2]
    candidates = [worktree / ".baseline/carousel-caption-bulk/snapshots",
                  worktree.parent / "carousel-caption-translation/.baseline/carousel-caption-bulk/snapshots"]
    fixture_dir = next((candidate for candidate in candidates if (candidate / "archive7d.json").is_file()), None)
    if fixture_dir is None:
        pytest.skip("Локальные сохранённые raw snapshots отсутствуют; постоянная hash-регрессия проверяется отдельно")
    filenames = {"archive7d": "archive7d.json", "archive14d": "archive14d.json", "live": "live-state.json"}
    raw = {name: (fixture_dir / filename).read_bytes() for name, filename in filenames.items()}
    data = {name: json.loads(value) for name, value in raw.items()}

    class ValidatingSnapshotStore(MemoryStore):
        def __init__(self):
            super().__init__(data)
            self.refs = {name: {"commit": str(index + 1) * 40, "blob": hashlib.sha1(raw[name]).hexdigest()}
                         for index, name in enumerate(worker.SOURCES)}

        def load(self):
            return deepcopy(self.state), None

        def save(self, state, expected):
            encoded = validate_state(state)
            assert len(json.loads(encoded)["items"]) == 2038
            if not self.writes:
                assert expected is None
            return super().save(state, expected)

    class NoApi:
        def get(self, *args, **kwargs):
            raise AssertionError("Офлайн startup не читает production API")

        def post(self, *args, **kwargs):
            raise AssertionError("Офлайн startup не вызывает модель")

    store = ValidatingSnapshotStore()
    status, result = execute(store, NoApi(), max_posts=0)
    assert status == "started"
    assert result == {"pending": 431, "not_needed": 1533, "empty": 74}
    assert store.state["inventory"] == {"eligible": 431, "not_needed": 1533, "empty": 74, "too_long": 0}
    assert len(store.writes) == 2
    assert store.state["discovered_day"] == "2026-10-23"
    assert {name: len(value["items"]) for name, value in data.items()} == {
        "archive7d": 1404, "archive14d": 1212, "live": 79,
    }
    assert all(set(item) == {"id", "source_hash", "periods", "status", "updated_at", "code", "inflight_started_at"}
               for item in store.state["items"])


def test_classifier_version_forces_one_rediscovery_for_unchanged_refs_and_day():
    store = MemoryStore()
    state = initial_state()
    assert worker.discover(store, state, NOW)
    assert not worker.discover(store, state, NOW)
    state.pop('classification_version')
    assert worker.discover(store, state, NOW)
    assert state['classification_version'] == 2
    assert not worker.discover(store, state, NOW)


@pytest.mark.parametrize('status', ['review', 'error', 'source_changed'])
@pytest.mark.parametrize('api_state,expected', [('ready', 'ready'), ('not_needed', 'not_needed'), ('available', None), ('unavailable', None)])
def test_previous_failure_reconciliation_only_gets_and_never_requeues(status, api_state, expected):
    store = MemoryStore()
    worker.discover(store, store.state, NOW)
    item = store.state['items'][0]
    item.update(status=status, code='failed')
    api = Api([{'source_hash': item['source_hash'], 'state': api_state, 'attempt': {'status': 'none'}}])
    execute(store, api)
    assert store.state['items'][0]['status'] == (expected or status)
    assert len(api.gets) == 1 and not api.posts


@pytest.mark.parametrize('status', ['review', 'error', 'source_changed', 'unknown', 'inflight'])
def test_rediscovery_keeps_attempt_history_until_read_only_reconcile(status):
    store = MemoryStore(snapshots(row(caption='ashrapov_67\n📷 UWW\nkazakhstan_wrestling')))
    state = initial_state()
    worker.discover(store, state, NOW)
    state['items'][0].update(status=status, code='failed', inflight_started_at=NOW.isoformat())
    state.pop('classification_version')
    assert worker.discover(store, state, NOW)
    assert state['items'][0]['status'] == status
    assert state['items'][0]['code'] == 'failed'
    assert state['items'][0]['inflight_started_at'] == NOW.isoformat()


def test_review_reconciliation_ignores_wrong_hash_without_post():
    store = MemoryStore()
    worker.discover(store, store.state, NOW)
    store.state['items'][0].update(status='review', code='failed')
    api = Api([{'source_hash': '0' * 64, 'state': 'ready'}])
    execute(store, api)
    assert store.state['items'][0]['status'] == 'review'
    assert not api.posts


@pytest.mark.parametrize('status,values,continuation', [
    ('started', {'pending': 2, 'ready': 3}, 'true'),
    ('started', {'ready': 3}, 'false'),
    ('started', {'pending': 0, 'review': 1}, 'false'),
    ('completed', {'ready': 4}, 'false'),
    ('paused', {'pending': 4}, 'false'),
    ('paused', {'pending': 4, 'unknown': 1}, 'false'),
    ('paused', {'review': 1}, 'false'),
])
def test_cli_continuation_is_fixed_boolean_only_for_started_pending(monkeypatch, tmp_path, status, values, continuation):
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_ENABLED', 'true')
    monkeypatch.setenv('GH_TOKEN', 'dummy')
    monkeypatch.setattr(worker, 'run', lambda *args, **kwargs: (status, values))
    assert worker.main() == 0
    lines = output.read_text().splitlines()
    assert lines[-1] == 'needs_continuation=' + continuation
    assert set(lines) <= {'needs_continuation=true', 'needs_continuation=false'}


def test_disabled_cli_does_not_schedule_continuation(monkeypatch, tmp_path):
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_ENABLED', 'false')
    monkeypatch.setattr(worker, 'run', lambda *args, **kwargs: pytest.fail('disabled не запускает worker'))
    assert worker.main() == 0
    assert output.read_text() == 'needs_continuation=false\n'


def test_failed_cli_continuation_stays_false_and_no_private_exception_leaks(monkeypatch, tmp_path, capsys):
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_ENABLED', 'true')
    monkeypatch.setenv('GH_TOKEN', 'dummy')
    def fail(*args, **kwargs):
        raise ValueError('PRIVATE_CAPTION')
    monkeypatch.setattr(worker, 'run', fail)
    assert worker.main() == 1
    assert output.read_text() == 'needs_continuation=false\n'
    assert 'PRIVATE_CAPTION' not in capsys.readouterr().out


@pytest.mark.parametrize('limit', ['', '0', '-1', '501', '1.0', 'true', ' 5', '5 ', '05', '1000'])
def test_cli_max_posts_rejects_invalid_before_startup_or_post(monkeypatch, tmp_path, limit):
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_ENABLED', 'true')
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_MAX_POSTS', limit)
    monkeypatch.setenv('GH_TOKEN', 'dummy')
    monkeypatch.setattr(worker, 'run', lambda *args, **kwargs: pytest.fail('invalid limit не запускает worker'))
    assert worker.main() == 1
    assert output.read_text() == 'needs_continuation=false\n'


@pytest.mark.parametrize('limit,expected', [(None, 500), ('1', 1), ('5', 5), ('500', 500)])
def test_cli_bounded_run_preserves_default_and_continuation_contract(monkeypatch, tmp_path, limit, expected):
    output = tmp_path / 'github-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('CAROUSEL_CAPTION_BULK_ENABLED', 'true')
    monkeypatch.setenv('GH_TOKEN', 'dummy')
    if limit is None:
        monkeypatch.delenv('CAROUSEL_CAPTION_BULK_MAX_POSTS', raising=False)
    else:
        monkeypatch.setenv('CAROUSEL_CAPTION_BULK_MAX_POSTS', limit)
    def run(*args, **kwargs):
        assert kwargs == {'max_posts': expected}
        return 'started', {'pending': 9}
    monkeypatch.setattr(worker, 'run', run)
    assert worker.main() == 0
    assert output.read_text().splitlines()[-1] == 'needs_continuation=true'


def test_five_post_run_keeps_batch_tail_pending_for_one_safe_continuation():
    store = MemoryStore(snapshots(*[row(str(index), f'English caption {index}') for index in range(1, 13)]))
    api = Api()
    status, result = execute(store, api, max_posts=5)
    assert status == 'started' and result == {'ready': 5, 'pending': 7}
    assert len(api.posts) == 5
    assert sum(item['code'] == 'batch_unsent' for item in store.state['items']) == 5
    for item in store.state['items']:
        assert item['inflight_started_at'] is None
    next_api = Api()
    status, result = execute(store, next_api)
    assert status == 'completed' and result == {'ready': 12}
    assert len(next_api.posts) == 7
