import json
from datetime import datetime, timedelta, timezone

from app.radar.activity_job import run_batch


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.payload


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append((url, headers, params, timeout))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def setup(tmp_path, names=('one',), state=None):
    (tmp_path / 'selection.json').write_text(json.dumps({
        'authors': [{'username': name, 'followers': None} for name in names],
        'start_date': '2026-10-15', 'end_date': '2026-10-17',
        'timezone': 'Europe/Moscow', 'total': len(names)}))
    if state is not None:
        (tmp_path / 'state.json').write_text(json.dumps(state))
    return tmp_path / 'report.json'


def batch(tmp_path, client, **kwargs):
    return run_batch(tmp_path, tmp_path / 'report.json', token='test-token', account_id='123',
                     client=client, clock=lambda: NOW, **kwargs)


def page(items, after=None, **kwargs):
    media = {'data': items}
    if after:
        media['paging'] = {'cursors': {'after': after}}
    return Response({'business_discovery': {'username': 'one', 'media_count': 100, 'media': media}}, **kwargs)


def item(number, stamp='2026-10-15T12:00:00+0000', kind='IMAGE'):
    return {'id': str(number), 'timestamp': stamp, 'media_type': kind}


def test_existing_checkpoint_is_reused_without_http(tmp_path):
    setup(tmp_path, ('one', 'two'), {'authors': {'one': {'status': 'available', 'coverage': 'partial',
                                                       'items': [item(1)], 'reason': 'reused_existing_collection'},
                                               'two': {'status': 'unavailable'}}, 'requests': 12,
                                   'started_at': NOW.isoformat(), 'last_usage': {}})
    client = Client()
    report = batch(tmp_path, client)
    assert client.calls == []
    assert report['collection']['status'] == 'finished'
    assert report['collection']['requests'] == 12
    assert report['totals']['observed_total'] == 1
    assert report['totals']['exact_total'] is None
    assert 'diagnostics' not in report


def test_budget_and_existing_cooldown_have_no_extra_http(tmp_path):
    setup(tmp_path, ('one', 'two'))
    client = Client(page([], None))
    report = batch(tmp_path, client, max_requests=1)
    assert len(client.calls) == 1
    assert report['collection']['status'] == 'running'
    assert report['collection']['pending'] == 1
    assert json.loads((tmp_path / 'state.json').read_text())['requests'] == 1
    state = json.loads((tmp_path / 'state.json').read_text())
    state['cooldown_until'] = (NOW + timedelta(minutes=10)).isoformat()
    (tmp_path / 'state.json').write_text(json.dumps(state))
    report = batch(tmp_path, Client())
    assert report['collection']['status'] == 'cooldown'
    assert report['collection']['requests'] == 1


def test_usage_cooldown_and_bearer_header(tmp_path):
    setup(tmp_path, ('one', 'two'))
    client = Client(page([item(1)], after='CUR', headers={'x-app-usage': json.dumps({'call_count': 2,
                                                                                      'total_cputime': 86,
                                                                                      'total_time': 3})}))
    report = batch(tmp_path, client)
    assert report['collection']['status'] == 'cooldown'
    assert datetime.fromisoformat(report['collection']['cooldown_until']) == NOW + timedelta(seconds=600)
    assert len(client.calls) == 1
    assert client.calls[0][1] == {'Authorization': 'Bearer test-token'}
    assert 'test-token' not in client.calls[0][0]
    assert client.calls[0][3] == 45
    assert '.limit(25)' in client.calls[0][2]['fields']
    saved = json.loads((tmp_path / 'state.json').read_text())
    assert saved['authors']['one']['after'] == 'CUR'
    assert saved['authors']['one']['items'] == [item(1)]


def test_rate_limit_and_retry_after_preserve_pending(tmp_path):
    setup(tmp_path)
    report = batch(tmp_path, Client(Response({'error': {'code': 4, 'message': 'secret'}}, 429,
                                            {'retry-after': '1200'})))
    state = json.loads((tmp_path / 'state.json').read_text())
    assert report['collection']['status'] == 'cooldown'
    assert datetime.fromisoformat(state['cooldown_until']) == NOW + timedelta(seconds=1200)
    assert state['authors']['one']['status'] == 'pending'
    assert 'secret' not in (tmp_path / 'state.json').read_text()


def test_auth_and_permissions_stop_without_unavailable(tmp_path):
    for code, expected in ((190, 'authentication_required'), (10, 'permissions_or_request_error'),
                           (200, 'permissions_or_request_error')):
        setup(tmp_path)
        report = batch(tmp_path, Client(Response({'error': {'code': code, 'message': 'secret'}}, 400)))
        assert report['collection']['status'] == expected
        assert report['collection']['unavailable'] == 0
        assert report['collection']['pending'] == 1
        assert 'secret' not in (tmp_path / 'state.json').read_text()


def test_resume_cursor_and_dedup_after_continuation_failure(tmp_path):
    setup(tmp_path)
    first = page([item(1)], after='CUR')
    failed = Response({'error': {'code': 4}}, 429)
    report = batch(tmp_path, Client(first, failed))
    assert report['collection']['status'] == 'cooldown'
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['authors']['one']['items'] == [item(1)]
    assert state['authors']['one']['next_page'] == 2
    state['cooldown_until'] = (NOW - timedelta(seconds=1)).isoformat()
    (tmp_path / 'state.json').write_text(json.dumps(state))
    client = Client(page([item(1), item(2)], None))
    report = batch(tmp_path, client)
    assert '.after(CUR)' in client.calls[0][2]['fields']
    assert report['totals']['observed_total'] == 2
    assert report['profiles'][0]['exact_total'] == 2
    assert len(json.loads((tmp_path / 'state.json').read_text())['authors']['one']['items']) == 2


def test_resume_works_with_only_portable_state_and_selection(tmp_path):
    setup(tmp_path, state={'authors': {'one': {'status': 'available', 'coverage': 'partial',
                                             'items': [item(1)], 'resume_required': True,
                                             'next_page': 2, 'after': 'OLD'}}, 'requests': 1})
    client = Client(page([item(2)]))
    report = batch(tmp_path, client)
    assert '.after(OLD)' in client.calls[0][2]['fields']
    assert report['totals']['observed_total'] == 2
    assert report['collection']['requests'] == 2


def test_confirmed_zero_differs_from_unavailable_and_unchecked(tmp_path):
    setup(tmp_path, ('one', 'two', 'three'))
    client = Client(page([]), Response({'error': {'code': 100}}, 400))
    report = batch(tmp_path, client, max_requests=2)
    assert report['collection']['available'] == 1
    assert report['collection']['unavailable'] == 1
    assert report['collection']['pending'] == 1
    assert report['profiles'][0]['exact_total'] == 0
    assert report['totals']['exact_total'] == 0  # Сводка только по доступным профилям.


def test_window_tail_is_observed_minimum(tmp_path):
    setup(tmp_path)
    old = [item(i, '2026-10-14T20:00:00+0000') for i in range(5)]
    report = batch(tmp_path, Client(page([item(10)] + old, after='NEXT')))
    assert report['collection']['status'] == 'finished'
    assert report['profiles'][0]['observed_total'] == 1
    assert report['profiles'][0]['exact_total'] is None
    assert report['profiles'][0]['coverage'] == {'posts': 'partial', 'reels': 'partial'}


def test_deadline_before_attempt_and_temporary_error_stay_pending(tmp_path):
    setup(tmp_path)
    client = Client()
    report = run_batch(tmp_path, tmp_path / 'report.json', token='x', account_id='123',
                       client=client, clock=lambda: NOW, max_seconds=0)
    assert report['collection']['status'] == 'running'
    assert report['collection']['requests'] == 0
    assert client.calls == []
    report = batch(tmp_path, Client(Response({'error': {'code': 2, 'is_transient': True}}, 500)))
    assert report['collection']['status'] == 'cooldown'
    assert report['collection']['unavailable'] == 0
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['authors']['one']['resume_required'] is True
    assert state['authors']['one']['next_page'] == 1


def test_network_error_is_retried_by_next_scheduled_batch(tmp_path):
    setup(tmp_path)
    report = batch(tmp_path, Client(OSError('Проверочная ошибка сети')))
    assert report['collection']['status'] == 'cooldown'
    retry = Client(page([item(1, kind='CAROUSEL_ALBUM')]))
    report = run_batch(tmp_path, tmp_path / 'report.json', token='x', account_id='123',
                       client=retry, clock=lambda: NOW + timedelta(minutes=6))
    assert report['collection']['status'] == 'finished'
    assert report['totals']['carousels'] == 1
    assert len(retry.calls) == 1


def test_missing_legacy_cursor_does_not_finish_pending_author(tmp_path):
    setup(tmp_path, state={'authors': {'one': {'status': 'pending', 'resume_required': True, 'items': []}}})
    client = Client(page([item(1)]))
    report = batch(tmp_path, client)
    assert len(client.calls) == 1
    assert report['collection']['checked'] == 1
    assert report['collection']['pending'] == 0
    assert report['collection']['status'] == 'finished'


def test_unknown_field_is_global_error_not_author_unavailable(tmp_path):
    setup(tmp_path)
    report = batch(tmp_path, Client(Response({'error': {'code': 100,
                                                       'message': 'Tried accessing nonexisting field secret'}}, 400)))
    assert report['collection']['status'] == 'permissions_or_request_error'
    assert report['collection']['unavailable'] == 0
    assert 'secret' not in (tmp_path / 'state.json').read_text()


def test_empty_page_with_cursor_is_partial_and_malformed_media_is_pending(tmp_path):
    setup(tmp_path)
    report = batch(tmp_path, Client(page([], after='NEXT')))
    assert report['profiles'][0]['exact_total'] is None
    assert report['profiles'][0]['observed_total'] == 0
    setup(tmp_path, state={'authors': {}, 'requests': 0})
    report = batch(tmp_path, Client(Response({'business_discovery': {'media_count': 5}})))
    assert report['collection']['status'] == 'failed'
    assert report['collection']['available'] == 0
    assert report['collection']['pending'] == 1


def test_one_author_continues_after_twenty_pages_across_batches(tmp_path):
    setup(tmp_path)
    first_pages = [page([item(number, '2026-10-18T12:00:00+0000')], after=f'CUR{number}')
                   for number in range(1, 21)]
    first_client = Client(*first_pages)
    first_report = batch(tmp_path, first_client, max_requests=20)
    state = json.loads((tmp_path / 'state.json').read_text())
    author = state['authors']['one']
    assert len(first_client.calls) == 20
    assert first_report['collection']['status'] == 'running'
    assert author['next_page'] == 21
    assert author['after'] == 'CUR20'
    assert len(author['items']) == 20
    assert author['resume_required'] is True
    assert first_report['profiles'][0]['exact_total'] is None

    old = [item(number, '2026-10-14T20:00:00+0000') for number in range(22, 27)]
    second_client = Client(page([item(21)], after='CUR21'), page(old, after='CUR22'))
    final_report = batch(tmp_path, second_client, max_requests=20)
    assert len(second_client.calls) == 2
    assert '.after(CUR20)' in second_client.calls[0][2]['fields']
    assert '.after(CUR21)' in second_client.calls[1][2]['fields']
    assert final_report['collection']['status'] == 'finished'
    assert final_report['collection']['requests'] == 22
    assert final_report['profiles'][0]['observed_total'] == 1
    assert final_report['profiles'][0]['exact_total'] is None
    final_state = json.loads((tmp_path / 'state.json').read_text())
    assert len(final_state['authors']['one']['items']) == 26
    assert final_state['authors']['one']['next_page'] == 23
    assert final_state['authors']['one'].get('needs_review') is not True


def test_repeated_cursor_stalls_partial_and_moves_to_next_author(tmp_path):
    setup(tmp_path, ('one', 'two'))
    client = Client(page([item(1)], after='SAME'),
                    page([item(2)], after='SAME'),
                    page([item(3)]))
    report = batch(tmp_path, client)
    assert len(client.calls) == 3
    assert report['collection']['status'] == 'finished'
    assert report['collection']['review_required'] == 1
    assert report['collection']['unavailable'] == 0
    assert report['profiles'][0]['observed_total'] == 2
    assert report['profiles'][0]['exact_total'] is None
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['authors']['one']['reason'] == 'pagination_stalled'
    assert state['authors']['one']['needs_review'] is True
    assert state['authors']['one']['resume_required'] is False
    assert batch(tmp_path, Client())['collection']['requests'] == 3


def test_duplicate_page_without_new_ids_stalls_even_with_new_cursor(tmp_path):
    setup(tmp_path)
    client = Client(page([item(1)], after='FIRST'), page([item(1)], after='SECOND'))
    report = batch(tmp_path, client)
    assert len(client.calls) == 2
    assert report['collection']['review_required'] == 1
    assert report['profiles'][0]['exact_total'] is None
    assert report['profiles'][0]['observed_total'] == 1
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['authors']['one']['items'] == [item(1)]


def test_legacy_page_above_twenty_resumes_without_requerying(tmp_path):
    setup(tmp_path, state={'authors': {'one': {'status': 'available', 'coverage': 'partial',
                                             'items': [item(1)], 'resume_required': True,
                                             'next_page': 21, 'after': 'PAGE20'}}, 'requests': 20})
    client = Client(page([item(2)]))
    report = batch(tmp_path, client)
    assert len(client.calls) == 1
    assert '.after(PAGE20)' in client.calls[0][2]['fields']
    assert report['collection']['requests'] == 21
    assert report['profiles'][0]['observed_total'] == 2


def test_missing_cursor_replays_two_pages_then_reaches_new_third_page(tmp_path):
    setup(tmp_path, state={'authors': {'one': {'status': 'available', 'coverage': 'partial',
                                             'items': [item(1), item(2)],
                                             'resume_required': True, 'next_page': 3,
                                             'after': None,
                                             'seen_cursors': ['CUR1', 'CUR2'],
                                             'traversal_ids': ['1', '2']}},
                           'requests': 2})
    client = Client(page([item(1)], after='CUR1'),
                    page([item(2)], after='CUR2'),
                    page([item(3)]))
    report = batch(tmp_path, client)
    assert len(client.calls) == 3
    assert '.after(CUR1)' in client.calls[1][2]['fields']
    assert '.after(CUR2)' in client.calls[2][2]['fields']
    assert report['collection']['status'] == 'finished'
    assert report['collection']['review_required'] == 0
    assert report['profiles'][0]['observed_total'] == 3
    assert report['profiles'][0]['exact_total'] == 3
    state = json.loads((tmp_path / 'state.json').read_text())
    assert len(state['authors']['one']['items']) == 3
    assert state['authors']['one'].get('needs_review') is not True
