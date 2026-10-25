"""Границы истории, фильтры и независимость очередей."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import carousel_favorites, carousels
from app.radar.carousel_window import history_window, date_window
from app.radar.live_carousel_job import run
from tests.test_live_carousel_job import MemoryStore, NOW
from tests.test_carousel_job import Client, detail, page


@pytest.mark.parametrize(('now', 'first'), [
    ('2026-10-25T12:00:00+00:00', '2026-06-25'),
    ('2026-06-30T12:00:00+00:00', '2026-02-28'),
    ('2024-06-30T12:00:00+00:00', '2024-02-29'),
    ('2026-01-31T12:00:00+00:00', '2025-09-30'),
    ('2026-10-24T21:00:00+00:00', '2026-06-25'),
])
def test_calendar_months(now, first):
    start, end = history_window(datetime.fromisoformat(now))
    assert start.date().isoformat() == first
    assert end.hour == 0 and end.tzinfo == start.tzinfo


def test_inclusive_day_uses_moscow_midnight():
    first, last = date_window('2026-07-01', '2026-07-01')
    assert first.astimezone(timezone.utc).isoformat() == '2026-06-30T21:00:00+00:00'
    assert last.astimezone(timezone.utc).isoformat() == '2026-07-01T21:00:00+00:00'


@pytest.fixture
def feed(monkeypatch):
    base = dict(username='alice', media_type='CAROUSEL_ALBUM', comments=3, likes=10)
    state = {'authors': [dict(username='alice', active=True, status='updated', history_status='complete',
                              history_from='2026-06-01T00:00:00+03:00', last_checked_at='2026-10-25T10:00:00Z')],
             'items': [dict(base, id='1', timestamp='2026-06-30T21:00:00Z'),
                       dict(base, id='2', timestamp='2026-07-01T20:59:59Z'),
                       dict(base, id='3', timestamp='2026-07-01T21:00:00Z'),
                       dict(base, id='4', timestamp='2026-07-01T10:00:00Z', likes=None),
                       dict(base, id='5', timestamp='2026-07-01T10:00:00Z', comments=1),
                       dict(base, id='6', username='hidden', timestamp='2026-07-01T10:00:00Z')]}
    class Store:
        def read_state(self):
            return state
    monkeypatch.setattr(carousel_favorites, '_store', lambda _: Store())
    app = FastAPI()
    app.include_router(carousels.router)
    return TestClient(app), state


def test_combined_ranges_before_pagination_and_unknown_counts(feed):
    client, _ = feed
    args = dict(history_days='4m', date_from='2026-07-01', date_to='2026-07-01',
                min_likes=0, max_likes=10, min_comments=2, max_comments=3, limit=1)
    first = carousel_favorites.live_page(**args)
    second = carousel_favorites.live_page(**args, offset=1)
    assert first['total'] == second['total'] == 2
    assert [first['items'][0]['id'], second['items'][0]['id']] == ['1', '2']
    assert first['coverage']['status'] == 'complete'
    assert client.post('/radar/carousels', json=dict(period='custom', date_from='2026-07-01',
        date_to='2026-07-01', min_likes=0, min_comments=2)).json()['total'] == 2


@pytest.mark.parametrize('filters', [
    dict(period='custom'), dict(period='custom', date_from='2026-07-01'),
    dict(period='custom', date_from='2026-08-01', date_to='2026-07-01'),
    dict(period='custom', date_from='2026-02-30', date_to='2026-03-01'),
    dict(min_likes=-1), dict(min_likes=10, max_likes=1), dict(min_comments=2, max_comments=0),
    dict(min_likes=True), dict(min_comments=1.5),
])
def test_invalid_filters_rejected(feed, filters):
    assert feed[0].post('/radar/carousels', json=filters).status_code == 422


def test_partial_history_not_empty_complete_and_custom_details_keep_old_items(feed):
    _, state = feed
    state['authors'][0]['history_status'] = 'collecting'
    result = carousel_favorites.live_page(history_days='4m', date_from='2025-01-01', date_to='2025-01-02')
    assert result['items'] == [] and result['coverage']['status'] == 'partial'
    state['items'].append(dict(state['items'][0], id='99', timestamp='2025-01-01T12:00:00Z'))
    assert carousel_favorites.live_detail('99', history_days='4m')['id'] == '99'


def execute(store, client, **kwargs):
    return run(store, client, token='test', account_id='123', clock=lambda: NOW, **kwargs)


def test_new_author_history_and_recent_have_separate_cursors_and_keep_old_items():
    store = MemoryStore()
    store.items['saved'] = {'id': 'saved'}
    execute(store, Client(page([detail('new')]), page([detail('july', timestamp='2026-07-01T12:00:00Z')], after='history')), max_requests=2)
    row = store.rows[0]
    assert row['checkpoint'] == {} and row['history_checkpoint']['after'] == 'history'
    assert store.pruned is None and {'saved', 'july'} <= store.items.keys()
    client = Client(page([]))
    execute(store, client, max_requests=2)
    assert '.after(history)' in client.calls[0][2]['fields']
    assert row['history_status'] == 'complete' and row['history_months'] == 4


def test_recent_not_starved_by_big_history_and_budget_is_bounded():
    store = MemoryStore([dict(username='old', added_at='1', history_checkpoint={'version':3, 'lane':'history',
                          'start':'2026-06-20T00:00:00+03:00', 'after':'older', 'seen':[], 'ids':[]}),
                         dict(username='new', added_at='2')])
    client = Client(page([detail('fresh')]), page([]), page([], after='next'), page([]))
    result = execute(store, client, max_requests=4)
    assert result['requests'] <= 4
    assert '.username(old)' in client.calls[0][2]['fields'] and '.after(older)' not in client.calls[0][2]['fields']
    assert '.username(new)' in client.calls[1][2]['fields']
    assert '.after(older)' in client.calls[2][2]['fields']


def test_old_month_coverage_is_not_four_months_and_error_preserves_cursor():
    store = MemoryStore([dict(username='one', history_days=30, coverage_version=2,
                             last_checked_at=NOW.isoformat(), next_check_at='2026-10-20T13:00:00Z')])
    client = Client(page([detail('july', timestamp='2026-07-01T12:00:00Z')], after='next'))
    execute(store, client, max_requests=1)
    assert store.rows[0]['coverage_version'] == 2
    assert store.rows[0]['history_checkpoint']['after'] == 'next'
    from tests.test_carousel_job import Response
    execute(store, Client(Response({}, 503)))
    assert store.rows[0]['history_checkpoint']['after'] == 'next'
    assert store.rows[0]['history_status'] == 'error'


def test_four_month_history_still_satisfies_legacy_month_view(monkeypatch):
    store = MemoryStore()
    execute(store, Client(page([]), page([])))
    row = dict(store.rows[0], active=True)
    assert row['history_days'] >= 120
    class State:
        def read_state(self):
            return {'authors': [row], 'items': []}
    monkeypatch.setattr(carousel_favorites, '_store', lambda _: State())
    result = carousel_favorites.live_page(history_days=30)
    assert result['collection']['status'] == 'finished'
    assert result['collection']['checked'] == 1


def test_all_saved_coverage_accounts_for_unknown_archive_authors(feed):
    _, state = feed
    archive = [dict(state['items'][0], id='100', username='other')]
    result = carousel_favorites.live_page(history_days='4m', favorites_only=False, archive_items=archive,
        date_from='2026-07-01', date_to='2026-07-01')
    assert result['coverage']['status'] == 'partial'
    assert result['coverage']['total_authors'] == 3
    assert result['coverage']['available_from'] is None


def test_recent_fills_gap_after_pause_longer_than_four_months():
    store = MemoryStore([dict(username='one', history_months=4, coverage_version=3, history_status='complete',
                             last_checked_at='2026-04-01T00:00:00Z')])
    execute(store, Client(page([detail('gap', timestamp='2026-05-01T12:00:00Z')])))
    assert 'gap' in store.items


def test_get_custom_and_archive_numeric_ranges(feed, monkeypatch):
    client, state = feed
    assert client.get('/radar/carousels?period=custom&date_from=2026-07-01&date_to=2026-07-01&min_likes=0&min_comments=2').json()['total'] == 2
    assert client.get('/radar/carousels?period=custom&date_from=2026-07-02&date_to=2026-07-01').status_code == 422
    async def report(period=None):
        return dict(items=state['items'], start_date='2026-07-01', end_date='2026-07-01')
    monkeypatch.setattr(carousels, '_selected_report', report)
    result = client.post('/radar/carousels', json=dict(period='archive14d', min_likes=0, max_likes=10, min_comments=2))
    assert result.status_code == 200 and result.json()['total'] == 4


def test_custom_archive_detail_outside_current_window(feed, monkeypatch):
    import asyncio
    _, state = feed
    async def report(period):
        return {'items':[dict(state['items'][0], id='888', timestamp='2020-01-01T12:00:00Z')]}
    monkeypatch.setattr(carousels, '_report', report)
    assert asyncio.run(carousels.resolve_detail('888', 'custom'))['id'] == '888'


def test_all_saved_coverage_keeps_pending_authors_without_cards(feed):
    _, state = feed
    state['authors'].append(dict(username='empty_pending', active=True, status='pending'))
    result = carousel_favorites.live_page(history_days='4m', favorites_only=False,
        date_from='2026-07-01', date_to='2026-07-01', author='empty_pending')
    assert result['total'] == 0 and result['coverage']['status'] == 'partial'
    assert result['coverage']['total_authors'] == 1


@pytest.mark.parametrize('period', ['4m', 'custom'])
def test_new_periods_are_accepted_by_translation_and_selection_contracts(period):
    from app.api.carousel_translation import TranslateCarousel
    from app.api.carousel_cover_translations import CoverRequest
    from app.api.carousel_likes import SelectCarousel
    assert TranslateCarousel(period=period, source_hash='a'*64).period == period
    assert CoverRequest(period=period).period == period
    assert SelectCarousel(period=period).period == period


def test_history_completion_does_not_claim_recent_checkpoint_finished():
    from app.radar.live_carousel_job import _run_lane
    store = MemoryStore([dict(username='one', last_checked_at=NOW.isoformat(),
                             checkpoint={'after': 'still-recent'}, status='collecting')])
    _run_lane(store, Client(page([])), token='test', account_id='123', clock=lambda: NOW, lane='history')
    assert store.rows[0]['history_status'] == 'complete'
    assert store.rows[0]['status'] == 'collecting'
    assert store.rows[0]['checkpoint'] == {'after':'still-recent'}
