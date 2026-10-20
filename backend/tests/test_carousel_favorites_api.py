from datetime import timedelta

import pytest
from app.api.carousel_favorites import username_from_profile, window


@pytest.mark.parametrize(('value', 'expected'), [
    ('@Alice.Name', 'alice.name'),
    ('https://www.instagram.com/Alice.Name/', 'alice.name'),
    ('https://instagram.com/alice.name/?igsh=share', 'alice.name'),
])
def test_profile_link_accepts_only_ig_profile(value, expected):
    assert username_from_profile(value) == expected


@pytest.mark.parametrize('value', [
    'https://evil.example/alice', 'http://instagram.com/alice',
    'https://instagram.com.evil.example/alice',
    'https://instagram.com/p/abc',
    'https://alice@instagram.com/bob',
    'https://instagram.com:444/alice',
    'https://instagram.com/alice#fragment',
])
def test_profile_link_rejects_untrusted_or_post_url(value):
    with pytest.raises(ValueError):
        username_from_profile(value)


def test_live_window_is_fourteen_moscow_calendar_days():
    start, end = window()
    assert start.utcoffset() == timedelta(hours=3)
    assert end - start == timedelta(days=14)
    assert start.hour == end.hour == 0


def test_shared_favorites_http_add_is_idempotent_and_remove_hides(monkeypatch):
    from copy import deepcopy

    from app.api import carousel_favorites, carousels
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = {'version': 1, 'authors': [], 'items': []}
    class FakeStore:
        def load(self):
            return deepcopy(state), 'current'
        def read_state(self):
            return deepcopy(state)
        def save(self, next_state, expected_sha):
            assert expected_sha == 'current'
            state.update(deepcopy(next_state))
            return 'next'
    monkeypatch.setattr(carousel_favorites, '_store', lambda client: FakeStore())
    app = FastAPI()
    app.include_router(carousel_favorites.router)
    app.include_router(carousels.router)
    client = TestClient(app)
    url = '/radar/carousels/favorites'
    assert client.post(url, json={'profile': 'https://instagram.com/Alice/'}).json()['username'] == 'alice'
    assert client.post(url, json={'profile': '@alice'}).status_code == 201
    assert len(state['authors']) == 1
    assert [row['username'] for row in client.get(url).json()['authors']] == ['alice']
    assert client.delete(url + '/alice').status_code == 204
    assert client.get(url).json()['authors'] == []
    assert client.post(url, json={'profile': '@alice'}).json()['status'] == 'pending'
    assert len(state['authors']) == 1


def test_live_feed_contains_only_active_favorites_and_filters_before_page(monkeypatch):
    from datetime import datetime, timezone

    from app.api import carousel_favorites

    stamp = datetime.now(timezone.utc).isoformat()
    state = {'version': 1, 'authors': [
        {'username': 'alice', 'active': True, 'status': 'updated', 'last_checked_at': stamp},
        {'username': 'bob', 'active': True, 'status': 'updated', 'last_checked_at': stamp},
        {'username': 'removed', 'active': False, 'status': 'updated', 'last_checked_at': stamp}],
        'items': [
            {'id': '1', 'username': 'alice', 'timestamp': stamp, 'media_type': 'CAROUSEL_ALBUM', 'likes': 2},
            {'id': '2', 'username': 'bob', 'timestamp': stamp, 'media_type': 'CAROUSEL_ALBUM', 'likes': 100},
            {'id': '3', 'username': 'alice', 'timestamp': stamp, 'media_type': 'CAROUSEL_ALBUM', 'likes': 20},
            {'id': '4', 'username': 'removed', 'timestamp': stamp, 'media_type': 'CAROUSEL_ALBUM', 'likes': 1000},
        ]}
    class FakeStore:
        def read_state(self):
            return state
    monkeypatch.setattr(carousel_favorites, '_store', lambda client: FakeStore())
    page = carousel_favorites.live_page(include_authors=['alice'], limit=1)
    assert page['total'] == 2 and [item['id'] for item in page['items']] == ['3']
    assert page['collection']['target'] == 2
    assert carousel_favorites.live_page(exclude_authors=['alice'])['total'] == 1


def test_all_saved_union_live_wins_and_filters_before_page(monkeypatch):
    from datetime import datetime, timezone
    from app.api import carousel_favorites as api
    now = datetime.now(timezone.utc).isoformat()
    base = {'timestamp': now, 'collected_at': now, 'media_type': 'CAROUSEL_ALBUM'}
    state = {'authors': [{'username': 'fav', 'active': True, 'status': 'pending'}],
             'items': [dict(base, id='1', username='fav', likes=2)]}
    class FakeStore:
        def read_state(self):
            return state
    monkeypatch.setattr(api, '_store', lambda client: FakeStore())
    history = [dict(base, id='1', username='fav', likes=100),
               dict(base, id='2', username='other', likes=10),
               dict(base, id='3', username='hidden', likes=50),
               dict(base, id='4', username='other', likes=1000, timestamp='2000-01-01T00:00:00Z')]
    on = api.live_page(archive_items=history)
    assert on['total'] == 1
    off = api.live_page(favorites_only=False, archive_items=history, exclude_authors=['hidden'], limit=1, offset=1)
    assert off['total'] == 2
    assert off['items'][0]['id'] == '1' and off['items'][0]['likes'] == 2
    assert off['collected_at'] == now
    assert api.live_page(favorites_only=False, archive_items=history, source_account={'other'})['total'] == 1
    assert api.live_page(favorites_only=False, archive_items=history, author='other')['items'][0]['id'] == '2'


def test_worker_api_windows_match_14_30_at_moscow_midnight():
    from datetime import datetime, timezone
    from app.api.carousel_favorites import window as api_window
    from app.radar.live_carousel_job import window as worker_window
    for hour, expected in [(20, '2026-09-20'), (21, '2026-09-21')]:
        now = datetime(2026, 10, 19, hour, 59, tzinfo=timezone.utc)
        for days in (14, 30):
            first, last = api_window(days, now)
            assert (first, last) == worker_window(now, days)
            assert (last - first).days == days
            if days == 30:
                assert first.date().isoformat() == expected


def test_30_day_feed_and_detail_include_older_live_without_changing_14_day(monkeypatch):
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.api import carousel_favorites as api
    stamp = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    row = {'id': '901', 'username': 'alice', 'media_type': 'CAROUSEL_ALBUM', 'timestamp': stamp, 'caption': 'Месячная история'}
    class FakeStore:
        def read_state(self):
            return {'authors': [{'username': 'alice', 'active': True, 'status': 'updated'}], 'items': [row]}
    monkeypatch.setattr(api, '_store', lambda client: FakeStore())
    assert api.live_page()['total'] == 0
    assert api.live_page(history_days=30)['total'] == 1
    assert api.live_detail('901', history_days=30)['caption'] == row['caption']
    with pytest.raises(HTTPException):
        api.live_detail('901')
