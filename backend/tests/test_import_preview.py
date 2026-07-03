from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_preview_all_new(monkeypatch):
    monkeypatch.setattr('app.api.import_._done_shortcodes', lambda shortcodes: set())

    body = {
        'links_text': (
            'https://www.instagram.com/reel/NEW001/\n'
            'https://www.instagram.com/p/NEW002/'
        ),
    }
    resp = client.post('/api/import/preview', json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data == {'total': 2, 'reels': 1, 'posts': 1, 'already_done': 0, 'new': 2}


def test_preview_some_already_done(monkeypatch):
    monkeypatch.setattr('app.api.import_._done_shortcodes', lambda shortcodes: {'DONE01'})

    body = {
        'links_text': (
            'https://www.instagram.com/reel/DONE01/\n'
            'https://www.instagram.com/reel/NEW003/\n'
            'https://www.instagram.com/p/NEW004/'
        ),
    }
    resp = client.post('/api/import/preview', json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data['total'] == 3
    assert data['reels'] == 2
    assert data['posts'] == 1
    assert data['already_done'] == 1
    assert data['new'] == 2


def test_preview_dedup_by_shortcode(monkeypatch):
    monkeypatch.setattr('app.api.import_._done_shortcodes', lambda shortcodes: set())

    body = {
        'links_text': (
            'https://www.instagram.com/reel/DUP005/\n'
            'https://www.instagram.com/reel/DUP005/?igshid=xyz\n'
            'https://www.instagram.com/p/DUP005/'
        ),
    }
    resp = client.post('/api/import/preview', json=body)
    assert resp.status_code == 200
    data = resp.json()
    # DUP005 встречается трижды под одним shortcode → parse_links схлопывает в 1 запись
    # (последнее вхождение побеждает: /p/ → post)
    assert data['total'] == 1
    assert data['posts'] == 1
    assert data['reels'] == 0
    assert data['new'] == 1


def test_preview_empty_links_text(monkeypatch):
    monkeypatch.setattr('app.api.import_._done_shortcodes', lambda shortcodes: set())

    resp = client.post('/api/import/preview', json={'links_text': ''})
    assert resp.status_code == 200
    data = resp.json()
    assert data == {'total': 0, 'reels': 0, 'posts': 0, 'already_done': 0, 'new': 0}
