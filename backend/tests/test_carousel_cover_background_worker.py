"""Проверки облачной очереди без сети и платных вызовов."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock

import httpx
import pytest

from app.services.carousel_cover_background_store import Conflict, initial_state, validate_state
from app.workers.carousel_cover_background import API_BASE, ProductionApi, merge_inventory, run

NOW = datetime(2026, 10, 23, 17, tzinfo=timezone.utc)


class Store:
    remaining = 5000
    reset_at = None

    def __init__(self, state=None, fail_at=None):
        self.state = deepcopy(state or initial_state())
        self.saves = []
        self.fail_at = fail_at

    def load(self):
        return deepcopy(self.state), str(len(self.saves))

    def save(self, state, sha):
        validate_state(state)
        if len(self.saves) == self.fail_at:
            raise Conflict("Конфликт")
        assert sha == str(len(self.saves))
        self.state = deepcopy(state)
        self.saves.append(deepcopy(state))
        return str(len(self.saves))


class Api:
    def __init__(self, statuses=None, result="ready", count=3):
        self.rows = [{"id": str(i + 1), "periods": ["30d", "14d"]} for i in range(count)]
        self.statuses = statuses or {}
        self.result = result
        self.posts = []
        self.reads = []
        self.discoveries = 0

    def inventory(self, mode, **kwargs):
        self.discoveries += 1
        return deepcopy(self.rows)

    def read(self, ids):
        self.reads.append(ids)
        return True, {identity: {"status": self.statuses.get(identity, "missing")} for identity in ids}, None

    def post(self, item):
        self.posts.append(item["id"])
        if isinstance(self.result, Exception):
            raise self.result
        return {"status": self.result, "http_ms": 123, "attempt_reserved": self.result not in {"busy", "running", "limit_reached"}}


def execute(store, api, **kwargs):
    return run(store, api, enabled=True, now_fn=lambda: NOW, monotonic=lambda: 0, log=lambda _: None, sleep_fn=lambda _: None, **kwargs)


def test_disabled_has_no_network_or_checkpoint():
    class Unused:
        def load(self):
            pytest.fail("disabled читает хранилище")
    assert run(Unused(), Unused()) == ("disabled", {})


def test_cache_before_post_and_terminal_cache_never_posts():
    api = Api({"1": "ready", "2": "no_text", "3": "error"})
    store = Store()
    status, values = execute(store, api)
    assert status == "completed" and values["remaining"] == 0
    assert not api.posts
    assert [item["status"] for item in store.state["items"]] == ["ready", "no_text", "review"]


def test_ten_item_checkpoint_uses_two_cas_writes_and_two_simultaneous_posts():
    api = Api(count=10)
    store = Store()
    lock, barrier = Lock(), Barrier(2)
    active = 0
    maximum = 0

    def post(item):
        nonlocal active, maximum
        checkpoint = store.saves[-1]
        assert sum(i["status"] == "inflight" for i in checkpoint["items"]) == 10
        assert next(i for i in checkpoint["items"] if i["id"] == item["id"])["status"] == "inflight"
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1
        return {"status": "ready", "http_ms": 10, "attempt_reserved": True}
    api.post = post
    assert execute(store, api)[0] == "completed"
    assert maximum == 2
    assert store.state["counters"]["posts"] == 10
    # Первые две записи — inventory и GET; вся группа использует один intent и один итог.
    assert len(store.saves) == 4
    assert all(item['status'] == 'inflight' for item in store.saves[2]['items'])
    assert all(item['status'] == 'ready' for item in store.saves[3]['items'])


def test_unknown_transport_stops_new_posts_and_never_retries_missing():
    api = Api(result=httpx.ReadTimeout("Таймаут"), count=10)
    store = Store()
    assert execute(store, api)[0] == "paused"
    assert len(api.posts) == 2
    unsent = [item for item in store.state['items'] if item['id'] not in api.posts]
    assert len(unsent) == 8
    assert all(item['status'] == 'pending' and item['first_inflight_at'] is None for item in unsent)
    unknown = {i["id"] for i in store.state["items"] if i["status"] == "unknown"}
    api.result = "ready"
    api.posts = []
    execute(store, api)
    assert not unknown.intersection(api.posts)
    assert all(i["status"] == "unknown" for i in store.state["items"] if i["id"] in unknown)
    api.statuses.update({identity: "ready" for identity in unknown})
    execute(store, api)
    assert all(i["status"] == "ready" for i in store.state["items"])


def test_crash_after_intent_never_resets_missing_to_pending():
    state = initial_state()
    merge_inventory(state, [{"id": "1", "periods": ["14d"]}], "existing", NOW)
    state["items"][0].update(status="inflight", first_inflight_at=NOW.isoformat())
    store, api = Store(state), Api(count=1)
    execute(store, api)
    assert store.state["items"][0]["status"] == "unknown" and not api.posts


def test_cas_failure_before_batch_prevents_post():
    api, store = Api(), Store(fail_at=2)
    with pytest.raises(Conflict):
        execute(store, api)
    assert not api.posts


@pytest.mark.parametrize("result", ["busy", "running", "limit_reached"])
def test_nonterminal_results_are_not_reposted_in_same_run(result):
    api, store = Api(result=result, count=1), Store()
    execute(store, api)
    assert api.posts == ["1"]
    api.posts = []
    execute(store, api)
    assert not api.posts
    if result == "limit_reached":
        assert store.state["pause_until"] == "2026-10-23T21:00:00+00:00"


def test_budget_cache_pauses_until_moscow_midnight_and_returns_without_sleep():
    api, store = Api({"1": "limit_reached"}, count=1), Store()
    assert execute(store, api)[0] == "paused"
    assert not api.posts
    read_count = len(api.reads)
    execute(store, api)
    assert len(api.reads) == read_count


def test_auth_unknown_stops_after_at_most_two_posts():
    from app.workers.carousel_cover_background import ApiFailure
    api, store = Api(result=ApiFailure(403, "api_http_error"), count=10), Store()
    assert execute(store, api)[0] == "paused"
    assert len(api.posts) <= 2 and store.state["pause_reason"] == "auth_failed"


def test_three_terminal_errors_stop_and_preserve_unsent():
    api, store = Api(result="error", count=10), Store()
    assert execute(store, api)[0] == "paused"
    assert 3 <= len(api.posts) <= 4
    assert store.state["pause_reason"] == "three_errors"
    assert any(i["status"] == "pending" for i in store.state["items"])


def test_existing_freezes_inventory_continuous_adds_and_preserves_history():
    api, store = Api(count=1), Store()
    execute(store, api, mode="existing")
    api.rows.append({"id": "2", "periods": ["30d"]})
    execute(store, api, mode="existing")
    assert len(store.state["items"]) == 1 and api.discoveries == 1
    execute(store, api, mode="continuous")
    assert len(store.state["items"]) == 2 and api.discoveries == 2
    assert api.posts == ["1", "2"]


def test_inventory_pages_deduplicates_and_disables_favorites_filter():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params["favorites_only"] == "false"
        assert request.url.params["limit"] == "100"
        offset = int(request.url.params["offset"])
        rows = [{"id": str(i)} for i in range(offset + 1, min(offset + 101, 102))]
        return httpx.Response(200, json={"items": rows, "total": 101})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        api = ProductionApi(client)
        rows = api.inventory("continuous")
        assert len(rows) == 101 and len(calls) == 8
        assert rows[0]["periods"] == ["30d", "14d", "archive14d", "7d"]
        calls.clear()
        assert len(api.inventory("archive")) == 101 and len(calls) == 2


def test_post_period_fallback_only_before_model_and_no_transport_retry():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "reelscribe-ai.vercel.app"
        if len(calls) == 1:
            return httpx.Response(422, json={})
        return httpx.Response(200, json={"item": {"id": "1", "status": "ready", "text_ru": "Секретный текст"}, "attempt_reserved": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ProductionApi(client).post({"id": "1", "periods": ["14d", "30d"]})
    assert result["status"] == "ready" and "text_ru" not in result
    assert [__import__("json").loads(r.content)["period"] for r in calls] == ["30d", "14d"]
    calls.clear()
    def uncertain(request):
        calls.append(request)
        raise httpx.ReadTimeout("Таймаут")
    with httpx.Client(transport=httpx.MockTransport(uncertain)) as client, pytest.raises(httpx.ReadTimeout):
        ProductionApi(client).post({"id": "1", "periods": ["30d", "14d"]})
    assert len(calls) == 1


def test_fixed_api_and_post_cap_and_deadline():
    with pytest.raises(ValueError):
        ProductionApi(None, "https://attacker.invalid")
    api, store = Api(count=5), Store()
    execute(store, api, max_posts=2)
    assert len(api.posts) == 2
    api, store = Api(), Store()
    execute(store, api, max_seconds=360)
    assert not api.posts


@pytest.mark.parametrize("stage", ["inventory", "read"])
def test_read_auth_failure_persists_reason_without_posts(stage):
    from app.workers.carousel_cover_background import ApiFailure
    api, store = Api(), Store()
    def denied(*args, **kwargs):
        raise ApiFailure(401, "api_http_error")
    setattr(api, stage, denied)
    assert execute(store, api)[0] == "paused"
    assert store.state["pause_reason"] == "auth_failed" and not api.posts


def test_archive_switch_preserves_other_history_but_only_processes_archive():
    api, store = Api(count=2), Store()
    merge_inventory(store.state, api.rows, "existing", NOW)
    api.rows = [{"id": "3", "periods": ["archive14d"]}]
    status, values = execute(store, api, mode="archive")
    assert status == "completed" and values["remaining"] == 0
    assert api.posts == ["3"] and len(store.state["items"]) == 3
    assert store.state["items"][0]["status"] == "pending"


def test_get_running_never_posts_even_if_next_get_is_missing():
    api, store = Api({"1": "running"}, count=1), Store()
    execute(store, api)
    api.statuses = {}
    execute(store, api)
    assert store.state["items"][0]["status"] == "unknown" and not api.posts


def test_budget_reset_timestamp_and_later_retry_are_preserved():
    api, store = Api(count=1), Store()
    api.read = lambda ids: (True, {"1": {"status": "limit_reached", "retry_after": "2026-10-24T22:00:00Z"}}, "2026-10-23T21:00:00Z")
    execute(store, api)
    assert store.state["pause_until"] == "2026-10-24T22:00:00+00:00" and not api.posts


def test_four_slow_period_fallbacks_share_300s_deadline_and_save_unknown_checkpoint():
    from app.workers.carousel_cover_background import POST_ITEM_SECONDS, REQUEST_DRAIN_SECONDS
    clock = [0.0]
    attempts = []
    def handler(request):
        timeout = request.extensions["timeout"]
        attempts.append((__import__("json").loads(request.content)["period"], timeout.copy()))
        assert timeout["connect"] <= 10 and timeout["write"] <= 10 and timeout["pool"] <= 5
        # Каждый предыдущий набор отвечает через80с; последний превышает остаток60с.
        elapsed = min(80, timeout["read"])
        clock[0] += elapsed
        if elapsed < 80:
            raise httpx.ReadTimeout("Общий deadline исчерпан", request=request)
        return httpx.Response(404 if len(attempts) % 2 else 422, json={})
    store = Store()
    inventory = Api(count=1)
    inventory.rows[0]["periods"] = ["30d", "14d", "archive14d", "7d"]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        production = ProductionApi(client, monotonic=lambda: clock[0])
        production.control = lambda: True
        production.inventory = inventory.inventory
        production.read = inventory.read
        status, values = execute(store, production)
        assert status == "paused" and values["unknown"] == 1
        # Следующий запуск может только GET; missing не возобновляет fallback/POST.
        execute(store, production)
    assert [p for p, _ in attempts] == ["30d", "14d", "archive14d", "7d"]
    assert [timeout["read"] for _, timeout in attempts] == [150, 150, 140, 60]
    assert clock[0] == POST_ITEM_SECONDS == 300 < REQUEST_DRAIN_SECONDS == 360
    assert store.saves[-1]["items"][0]["status"] == "unknown"
    assert store.saves[-1]["pause_reason"] is None  # Второй запуск read-only сверил неизвестный исход.
    assert any(s["pause_reason"] == "post_outcome_unknown" for s in store.saves)
    assert any(s["items"][0]["status"] == "inflight" for s in store.saves)


def test_not_needed_is_terminal_without_post():
    api, store = Api({'1': 'not_needed'}, count=1), Store()
    assert execute(store, api)[0] == 'completed'
    assert not api.posts and store.state['items'][0]['status'] == 'not_needed'
    assert store.state['counters']['cache_hits'] == 1


def test_cache_and_rejections_do_not_consume_new_attempt_cap():
    api, store = Api(count=4), Store()
    def post(item):
        api.posts.append(item['id'])
        return {'status': 'ready', 'http_ms': 1, 'attempt_reserved': item['id'] in {'3', '4'}}
    api.post = post
    execute(store, api, max_posts=1)
    assert api.posts == ['1', '2', '3']
    assert store.state['counters']['attempts_reserved'] == 1
    assert store.state['counters']['http_requests'] == 3
    assert store.state['counters']['cache_hits'] == 2
    assert store.state['items'][3]['status'] == 'pending'


def test_first_wave_busy_waits_gets_and_returns_next_eight_pending_without_post():
    api, store = Api(result='busy', count=10), Store()
    delays = []
    api.last_budget = {'available_slots': 2}
    # GET сообщает исчерпанную ёмкость после первой волны.
    original = api.read
    def read(ids):
        if api.posts:
            api.last_budget = {'available_slots': 0}
        return original(ids)
    api.read = read
    status, values = run(store, api, enabled=True, now_fn=lambda: NOW, monotonic=lambda: 0,
                         sleep_fn=delays.append, log=lambda _: None)
    assert status == 'paused' and delays == [5, 10, 20]
    assert len(api.posts) == 2 and api.reads[1:]
    assert store.state['counters']['attempts_reserved'] == 0
    assert store.state['counters']['busy_rejections'] == 2
    assert store.state['counters']['http_requests'] == 2
    assert all(i['status'] == 'pending' for i in store.state['items'])
    unsent = [item for item in store.state['items'] if item['id'] not in api.posts]
    assert len(unsent) == 8 and all(item['first_inflight_at'] is None for item in unsent)
    # В исходном intent все10 были защищены от crash; живой процесс вернул хвост до паузы.
    assert all(item['status'] == 'inflight' for item in store.saves[2]['items'])
    assert all(item['status'] == 'pending' for item in store.saves[3]['items'])
    assert store.state['pause_reason'] == 'busy_backpressure'
    assert not values['needs_continuation']


def test_racy_capacity_reports_still_bound_busy_retries_to_three_waits():
    api, store = Api(result='busy', count=10), Store()
    api.last_budget = {'available_slots': 2}
    delays = []
    run(store, api, enabled=True, now_fn=lambda: NOW, monotonic=lambda: 0,
        sleep_fn=delays.append, log=lambda _: None)
    assert delays == [5, 10, 20]
    assert len(api.posts) == 8  # Две исходных выдачи + три проверенные волны.
    assert store.state['pause_reason'] == 'busy_backpressure'


def test_controller_disabled_before_next_batch_stops_new_ids():
    api, store = Api(count=10), Store()
    api.control = lambda: len(api.posts) < 2
    execute(store, api)
    assert len(api.posts) == 2 and store.state['pause_reason'] == 'controller_disabled'
    assert sum(i['status'] == 'pending' for i in store.state['items']) == 8


def test_time_budget_with_progress_requests_continuation_without_cron():
    clock = [0.0]
    api, store = Api(count=10), Store()
    def post(item):
        api.posts.append(item['id'])
        clock[0] = 4500
        return {'status': 'ready', 'http_ms': 1, 'attempt_reserved': True}
    api.post = post
    status, values = run(store, api, enabled=True, now_fn=lambda: NOW, monotonic=lambda: clock[0], log=lambda _: None)
    assert status == 'started' and values['needs_continuation']
    assert len(api.posts) == 2 and values['pending'] == 8
    assert not store.state['pause_reason']


def test_no_progress_empty_or_unknown_only_never_requests_continuation():
    api, store = Api(count=0), Store()
    assert not execute(store, api)[1]['needs_continuation']
    api, store = Api(count=1), Store()
    merge_inventory(store.state, api.rows, 'existing', NOW)
    store.state['items'][0].update(status='unknown', first_inflight_at=NOW.isoformat())
    assert not execute(store, api)[1]['needs_continuation'] and not api.posts


def test_provider_rate_limit_pauses_new_ids_and_preserves_paid_error_terminal():
    api, store = Api(count=10), Store()
    def post(item):
        api.posts.append(item['id'])
        return {'status': 'error', 'reason_code': 'provider_rate_limit', 'retry_after': '2026-10-23T17:02:00Z',
                'http_ms': 1, 'attempt_reserved': True}
    api.post = post
    execute(store, api)
    assert len(api.posts) == 2 and store.state['counters']['attempts_reserved'] == 2
    assert store.state['pause_reason'] == 'provider_rate_limit'
    assert store.state['pause_until'] == '2026-10-23T17:02:00+00:00'
    assert all(i['status'] == 'review' for i in store.state['items'] if i['id'] in api.posts)
    before = api.posts[:]
    execute(store, api)
    assert api.posts == before


def test_crash_after_first_wave_keeps_group_intent_get_only_without_repeating_posts():
    api, store = Api(count=10), Store()
    barrier = Barrier(2)
    def crash(item):
        api.posts.append(item['id'])
        barrier.wait(timeout=3)
        raise SystemExit('Имитируем обрыв процесса до итогового CAS')
    api.post = crash
    with pytest.raises(SystemExit):
        execute(store, api)
    assert len(api.posts) == 2
    assert all(item['status'] == 'inflight' for item in store.state['items'])
    assert len(store.saves) == 3  # inventory, GET и общий intent; итог ещё не записан.
    recovered = Api(count=10)
    execute(store, recovered)
    assert not recovered.posts
    assert all(item['status'] == 'unknown' for item in store.state['items'])
    assert store.state['counters']['attempts_reserved'] == 0


@pytest.mark.parametrize('kind', ['limit', 'provider'])
def test_first_wave_pause_returns_eight_unsent_without_any_post(kind):
    api, store = Api(count=10), Store()
    def post(item):
        api.posts.append(item['id'])
        if kind == 'limit':
            return {'status': 'limit_reached', 'attempt_reserved': False, 'http_ms': 1}
        return {'status': 'error', 'reason_code': 'provider_rate_limit', 'retry_after': '2026-10-23T17:02:00Z',
                'attempt_reserved': True, 'http_ms': 1}
    api.post = post
    assert execute(store, api)[0] == 'paused'
    assert len(api.posts) == 2
    unsent = [item for item in store.state['items'] if item['id'] not in api.posts]
    assert len(unsent) == 8
    assert all(item['status'] == 'pending' and item['first_inflight_at'] is None for item in unsent)
    assert len(store.saves) == 4
    assert store.state['counters']['attempts_reserved'] == (0 if kind == 'limit' else 2)


def test_checkpoint_logs_github_quota_metadata_without_request_or_credentials():
    api, store = Api(count=10), Store()
    store.reset_at = NOW + timedelta(hours=1)
    logs = []
    run(store, api, enabled=True, now_fn=lambda: NOW, monotonic=lambda: 0,
        sleep_fn=lambda _: None, log=logs.append)
    checkpoints = [line for line in logs if line.startswith('checkpoint ')]
    assert len(checkpoints) == 4
    assert all('github_remaining=5000' in line and 'github_reset_at=2026-10-23T18:00:00+00:00' in line for line in checkpoints)
    assert not any(word in '\n'.join(logs).lower() for word in ('authorization', 'bearer', 'token', 'https://'))
