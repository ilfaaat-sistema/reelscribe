from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app.radar.live_carousel_job import run, window

from tests.test_carousel_job import Client, Response, detail, page

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [{'username': 'one', 'added_at': '2026-10-20'}])
        self.items = {}
        self.pruned = None

    def authors(self):
        return deepcopy(self.rows)

    def active(self, name):
        return any(row['username'] == name for row in self.rows)

    def update(self, name, **values):
        for row in self.rows:
            if row['username'] == name:
                row.update(deepcopy(values))

    def save(self, items):
        self.items.update({item['id']: item for item in items})

    def pause(self, until, reason):
        for row in self.rows:
            row.update(status='meta_paused', next_check_at=until, error=reason)

    def prune(self, start):
        self.pruned = start


def collect(store, client, **kwargs):
    return run(store, client, token='test', account_id='123', clock=lambda: NOW, **kwargs)


def test_moscow_window_and_filter():
    start, end = window(datetime(2026, 10, 19, 22, tzinfo=timezone.utc))
    assert start.isoformat() == '2026-10-07T00:00:00+03:00'
    assert end.isoformat() == '2026-10-21T00:00:00+03:00'
    store = MemoryStore()
    collect(store, Client(page([detail('1'), detail('2', timestamp='2026-10-01T12:00:00+0000')])))
    assert set(store.items) == {'1'}
    assert store.rows[0]['status'] == 'updated'
    assert store.rows[0]['checkpoint'] == {}


def test_checkpoint_resume_and_dedup():
    store = MemoryStore()
    collect(store, Client(page([detail('1')], after='next')), max_requests=1)
    assert store.rows[0]['checkpoint']['after'] == 'next'
    client = Client(page([detail('1'), detail('2')]))
    collect(store, client)
    assert '.after(next)' in client.calls[0][2]['fields']
    assert set(store.items) == {'1', '2'}
    assert store.rows[0]['status'] == 'updated'


def test_new_author_priority():
    store = MemoryStore([{'username': 'old', 'added_at': '1', 'last_checked_at': NOW.isoformat()},
                         {'username': 'new', 'added_at': '2'}])
    client = Client(page([]))
    collect(store, client, max_requests=1)
    assert '.username(new)' in client.calls[0][2]['fields']


def test_regular_check_reads_only_recent_overlap():
    store = MemoryStore([{'username': 'one', 'added_at': '1',
                          'last_checked_at': (NOW - timedelta(minutes=15)).isoformat()}])
    collect(store, Client(page([detail('old', timestamp='2026-10-18T12:00:00+0000')])))
    assert store.items == {}
    assert store.rows[0]['status'] == 'updated'


def test_usage_pause_is_global_and_blocks_new_author():
    store = MemoryStore()
    client = Client(page([detail('1')], after='more', headers={'x-app-usage': '{"call_count":85}'}))
    result = collect(store, client)
    assert result['status'] == 'meta_paused'
    assert store.rows[0]['checkpoint']['after'] == 'more'
    store.rows.append({'username': 'new'})
    assert collect(store, Client())['requests'] == 0
    store.rows[0]['next_check_at'] = (NOW - timedelta(seconds=1)).isoformat()
    assert collect(store, Client(page([]), page([])))['requests'] == 2


def test_429_retry_after_and_190_stop_all():
    for response, reason in [(Response({'error': {'code': 4}}, 429, {'retry-after': '3600'}), 'meta_rate_limit'),
                             (Response({'error': {'code': 190}}, 400), 'authentication_required')]:
        store = MemoryStore([{'username': 'one'}, {'username': 'two'}])
        client = Client(response)
        collect(store, client)
        assert len(client.calls) == 1
        assert all(row['status'] == 'meta_paused' and row['error'] == reason for row in store.rows)
        assert store.rows[0]['next_check_at'] == (NOW + timedelta(hours=1)).isoformat()


def test_bad_author_does_not_block_next():
    store = MemoryStore([{'username': 'one'}, {'username': 'two'}])
    collect(store, Client(Response({'error': {'code': 100}}, 400), page([])))
    assert [row['status'] for row in store.rows] == ['unavailable', 'updated']


def test_repeated_cursor_and_retryable_server_error():
    store = MemoryStore()
    collect(store, Client(page([detail('1')], after='same'), page([detail('2')], after='same')))
    assert store.rows[0]['error'] == 'pagination_stalled'
    store = MemoryStore()
    collect(store, Client(Response({}, 503)))
    assert store.rows[0]['error'] == 'temporary_meta_error'
    assert store.rows[0]['next_check_at']


def test_deleted_author_is_not_requested():
    store = MemoryStore()
    store.active = lambda name: False
    assert collect(store, Client())['requests'] == 0
