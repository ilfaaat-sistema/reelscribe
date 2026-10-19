import json
from datetime import datetime, timedelta, timezone

from app.radar.carousel_job import run_batch

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


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
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def setup(tmp_path, names=('one',), state=None):
    (tmp_path / 'selection.json').write_text(json.dumps({
        'authors': [{'username': name} for name in names],
        'start_date': '2026-10-13', 'end_date': '2026-10-19',
        'timezone': 'Europe/Moscow', 'total': len(names),
    }))
    if state is not None:
        (tmp_path / 'state.json').write_text(json.dumps(state))


def batch(tmp_path, client, **kwargs):
    return run_batch(tmp_path, tmp_path / 'report.json', token='test-token',
                     account_id='123', client=client, clock=lambda: NOW, **kwargs)


def page(items, after=None, media_count=100, followers=321, **response):
    media = {'data': items}
    if after is not None:
        media['paging'] = {'cursors': {'after': after}}
    return Response({'business_discovery': {'media_count': media_count,
                                             'followers_count': followers,
                                             'media': media}}, **response)


def item(identity, stamp='2026-10-19T12:00:00+0000', kind='CAROUSEL_ALBUM', **fields):
    return {'id': str(identity), 'media_type': kind, 'timestamp': stamp, **fields}


def detail(identity, **fields):
    return item(identity, permalink=f'https://www.instagram.com/p/code{identity}/',
                media_url='https://scontent.cdninstagram.com/photo.jpg',
                thumbnail_url='https://evil.example/photo.jpg',
                like_count=7, comments_count=0, view_count=None, **fields)


def test_minimal_scan_then_nested_detail_only_for_carousel_page(tmp_path):
    setup(tmp_path)
    client = Client(page([item(1, kind='IMAGE'), item(2)], after='CUR'),
                    page([detail(2), detail(1, media_type='IMAGE')], after='CUR'),
                    page([item(3, kind='VIDEO')]))
    report = batch(tmp_path, client)
    assert report['collection']['status'] == 'finished'
    assert report['collection']['requests'] == 3
    assert report['profiles'] == [{'username': 'one', 'carousels': 1}]
    assert report['totals'] == {'carousels': 1}
    assert len(report['items']) == 1
    result = report['items'][0]
    assert result['id'] == '2' and result['shortcode'] == 'code2'
    assert result['likes'] == 7 and result['comments'] == 0
    assert result['views'] is None and result['followers'] == 321
    assert result['thumbnail_url'] is None
    assert 'caption' not in client.calls[0][2]['fields']
    assert 'caption' in client.calls[1][2]['fields']
    assert 'caption' not in client.calls[2][2]['fields']
    assert 'view_count' not in client.calls[1][2]['fields']
    assert '.after(CUR)' not in client.calls[1][2]['fields']
    assert '.after(CUR)' in client.calls[2][2]['fields']
    assert all(call[0] == 'https://graph.facebook.com/v23.0/123' for call in client.calls)
    assert all(call[1] == {'Authorization': 'Bearer test-token'} for call in client.calls)


def test_moscow_window_excludes_both_outer_boundaries(tmp_path):
    setup(tmp_path)
    # 12 октября 21:00 UTC = 13 октября 00:00 в Москве.
    scan = [item(1, '2026-10-12T20:59:59+0000'),
            item(2, '2026-10-12T21:00:00+0000'),
            item(3, '2026-10-19T20:59:59+0000'),
            item(4, '2026-10-19T21:00:00+0000')]
    expanded = [dict(detail(raw['id']), timestamp=raw['timestamp']) for raw in scan]
    report = batch(tmp_path, Client(page(scan), page(expanded)))
    assert {entry['id'] for entry in report['items']} == {'2', '3'}
    assert report['totals']['carousels'] == 2


def test_detail_queue_resumes_after_budget_and_cooldown(tmp_path):
    setup(tmp_path)
    scan = page([item(1)], after='NEXT')
    older = [item(number, '2026-10-12T20:00:00+0000') for number in range(2, 7)]
    first = batch(tmp_path, Client(scan), max_requests=1)
    assert first['collection']['status'] == 'running'
    assert first['profiles'][0]['carousels'] == 0
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['authors']['one']['detail_pages'] == [{'after': None, 'ids': ['1']}]
    throttled = batch(tmp_path, Client(Response({'error': {'code': 4, 'message': 'secret'}}, 429,
                                                {'retry-after': '1200'})))
    assert throttled['collection']['status'] == 'cooldown'
    assert throttled['collection']['requests'] == 2
    assert 'secret' not in (tmp_path / 'state.json').read_text()
    assert batch(tmp_path, Client())['collection']['requests'] == 2
    resumed = run_batch(tmp_path, tmp_path / 'report.json', token='test-token',
                        account_id='123', client=Client(page([detail(1)]), page(older, after='OLD')),
                        clock=lambda: NOW + timedelta(minutes=21))
    assert resumed['collection']['status'] == 'finished'
    assert resumed['profiles'][0]['carousels'] == 1
    assert resumed['collection']['requests'] == 4


def test_priority_resumes_existing_checkpoint_without_changing_selection(tmp_path):
    setup(tmp_path, names=('one', 'two', 'three'), state={
        'authors': {'one': {'status': 'available', 'scan_complete': True,
                            'detail_pages': [], 'details': {}}},
        'requests': 1,
    })
    client = Client(page([]), page([]))
    report = batch(tmp_path, client, priority_authors='three,one')
    assert report['collection']['status'] == 'finished'
    assert report['collection']['checked'] == 3
    assert [entry['username'] for entry in report['profiles']] == ['one', 'two', 'three']
    assert 'username(three)' in client.calls[0][2]['fields']
    assert 'username(two)' in client.calls[1][2]['fields']
    assert json.loads((tmp_path / 'selection.json').read_text())['authors'][0]['username'] == 'one'


def test_deadline_before_every_http_and_restart_does_not_duplicate(tmp_path):
    setup(tmp_path)
    first = batch(tmp_path, Client(page([item(1)])), max_requests=1)
    assert first['collection']['status'] == 'running'
    assert first['items'] == []
    no_request = Client()
    result = batch(tmp_path, no_request, max_seconds=0)
    assert result['collection']['requests'] == 1
    assert no_request.calls == []
    client = Client(page([detail(1)]))
    final = batch(tmp_path, client)
    assert final['collection']['status'] == 'finished'
    assert final['totals']['carousels'] == 1
    assert batch(tmp_path, Client())['collection']['requests'] == 2


def test_unchecked_is_null_confirmed_empty_is_zero(tmp_path):
    setup(tmp_path, ('one', 'two', 'three'))
    report = batch(tmp_path, Client(page([]), Response({'error': {'code': 100}}, 400)), max_requests=2)
    assert report['collection']['checked'] == 2
    assert report['collection']['available'] == 1
    assert report['collection']['unavailable'] == 1
    assert report['collection']['pending'] == 1
    assert report['profiles'] == [
        {'username': 'one', 'carousels': 0},
        {'username': 'two', 'carousels': None},
        {'username': 'three', 'carousels': None},
    ]


def test_repeated_cursor_stops_with_review_and_keeps_unique_details(tmp_path):
    setup(tmp_path)
    report = batch(tmp_path, Client(page([item(1)], after='SAME'),
                                    page([detail(1)]),
                                    page([item(1)], after='SAME')))
    assert report['collection']['status'] == 'finished'
    assert report['collection']['review_required'] == 1
    assert report['totals']['carousels'] == 1
    assert report['collection']['requests'] == 3


def test_global_auth_and_field_errors_do_not_mark_profile_unavailable(tmp_path):
    for code, message, expected in ((190, 'secret', 'authentication_required'),
                                    (10, 'secret', 'permissions_or_request_error'),
                                    (100, 'Tried accessing nonexisting field secret',
                                     'permissions_or_request_error')):
        setup(tmp_path, state={'authors': {}, 'requests': 0})
        report = batch(tmp_path, Client(Response({'error': {'code': code, 'message': message}}, 400)))
        assert report['collection']['status'] == expected
        assert report['collection']['unavailable'] == 0
        assert report['profiles'][0]['carousels'] is None
        assert 'secret' not in (tmp_path / 'state.json').read_text()


def test_network_error_and_malformed_media_remain_retriable(tmp_path):
    setup(tmp_path)
    result = batch(tmp_path, Client(OSError('secret')))
    assert result['collection']['status'] == 'cooldown'
    assert result['collection']['pending'] == 1
    assert 'secret' not in (tmp_path / 'state.json').read_text()
    state = json.loads((tmp_path / 'state.json').read_text())
    state['cooldown_until'] = (NOW - timedelta(seconds=1)).isoformat()
    (tmp_path / 'state.json').write_text(json.dumps(state))
    result = batch(tmp_path, Client(Response({'business_discovery': {'media_count': 3}})))
    assert result['collection']['status'] == 'failed'
    assert result['collection']['pending'] == 1


def test_old_activity_checkpoint_cannot_be_used_as_new_dataset(tmp_path):
    setup(tmp_path, state={'authors': {'one': {'status': 'available', 'items': [item(1)]}},
                           'requests': 1})
    try:
        batch(tmp_path, Client())
    except ValueError as exc:
        assert 'Checkpoint другого сбора' in str(exc)
    else:
        raise AssertionError('Старый checkpoint должен быть отклонён')


def test_checkpoint_preserves_controller_flags(tmp_path):
    setup(tmp_path, state={'authors': {}, 'requests': 0,
                           'full_collection_enabled': 'unknown'})
    batch(tmp_path, Client(page([])))
    saved = json.loads((tmp_path / 'state.json').read_text())
    assert saved['full_collection_enabled'] == 'unknown'
    assert saved['job_kind'] == 'carousels_week'


def test_public_caption_does_not_break_checkpoint_secret_guard(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('checkpoint_store', Path(__file__).resolve().parents[2] / 'scripts/author_activity_store.py')
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    setup(tmp_path)
    report = batch(tmp_path, Client(page([item(1)]), page([detail(1, caption='Инструкция: Bearer example и access_token=example')])) )
    store.validate_data(report)
    store.validate_data(json.loads((tmp_path / 'state.json').read_text()))
    assert report['collection']['redactions'] == 2
    assert report['items'][0]['caption'] == 'Инструкция: [маркер скрыт] и [маркер скрыт]'
