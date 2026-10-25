"""Избранное расширяет видимый список, сохраняя независимость очереди Reels."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import radar_intel
from app.radar.carousel_sources import merge_sources


def base_sources():
    return {'active': [{'username': 'manual', 'reels_count': 9, 'needs_deep': True}],
            'excluded': [{'username': 'excluded', 'excluded_at': '2026-10-01'}], 'max_sources': 100}


def test_union_preserves_reels_and_deduplicates_favorites_without_mutation():
    base = base_sources()
    original = deepcopy(base)
    authors = [{'username': 'manual', 'status': 'updated', 'last_checked_at': '2026-10-25',
                'history_status': 'complete'}, {'username': 'favorite', 'status': 'pending'},
               {'username': 'favorite', 'status': 'pending'}, {'username': 'removed', 'active': False}]
    result = merge_sources(base, authors)
    rows = {row['username']: row for row in result['active']}
    assert len(rows) == 2
    assert rows['manual']['tracking_reels'] and rows['manual']['tracking_carousels']
    assert rows['manual']['reels_count'] == 9 and rows['manual']['needs_deep']
    assert rows['manual']['history_status'] == 'complete'
    assert rows['manual']['last_checked_at'] == '2026-10-25'
    assert not rows['favorite']['tracking_reels'] and not rows['favorite']['needs_deep']
    assert rows['favorite']['reels_count'] is None and rows['favorite']['last_scraped_at'] is None
    assert base == original


def test_excluded_manual_origin_survives_favorite_and_removal():
    base = base_sources()
    active = merge_sources(base, [{'username': 'excluded', 'status': 'pending'}])
    assert active['excluded'] == []
    row = next(row for row in active['active'] if row['username'] == 'excluded')
    assert row['source_excluded'] and row['excluded_at'] == '2026-10-01'
    assert not row['tracking_reels'] and row['tracking_carousels']
    assert merge_sources(base, [])['excluded'][0]['username'] == 'excluded'
    assert merge_sources(base, [])['active'][0]['tracking_reels']


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(radar_intel.feed, 'build_sources', base_sources)
    app = FastAPI()
    app.include_router(radar_intel.router)
    return TestClient(app)


def test_sources_survive_favorites_failure_without_leaking_internal_error(client, monkeypatch):
    def fail():
        raise RuntimeError('Внутренний ответ хранилища')
    monkeypatch.setattr(radar_intel.carousel_favorites, 'list_favorites', fail)
    response = client.get('/radar/sources')
    assert response.status_code == 200
    assert response.json()['active'][0]['tracking_reels']
    assert response.json()['carousel_tracking_error']
    assert 'Внутренний' not in response.text


def test_remove_reels_does_not_remove_carousel_favorite(client, monkeypatch):
    base = base_sources()
    favorites = [{'username': 'manual', 'status': 'pending'}]
    monkeypatch.setattr(radar_intel.feed, 'build_sources', lambda: deepcopy(base))
    monkeypatch.setattr(radar_intel.carousel_favorites, 'list_favorites', lambda: {'authors': favorites})
    def exclude(username):
        base['active'] = [row for row in base['active'] if row['username'] != username]
        base['excluded'].append({'username': username})
    monkeypatch.setattr(radar_intel.sources, 'exclude_source', exclude)
    assert client.delete('/radar/sources/manual').status_code == 200
    result = client.get('/radar/sources').json()
    assert len(result['active']) == 1
    assert result['active'][0]['tracking_carousels'] and not result['active'][0]['tracking_reels']
    assert favorites == [{'username': 'manual', 'status': 'pending'}]
