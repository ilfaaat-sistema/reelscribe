"""Проверки HTTP-контракта без сети; реальный SQL проверяется отдельно перед публикацией."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.hidden_authors import router
from app.services import hidden_authors as service

DATE = '2026-10-20T10:00:00Z'
URL = '/api/radar/hidden-authors'


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = False

    def table(self, name):
        assert name == 'carousel_hidden_authors'
        self.rpc_args = None
        return self

    def select(self, columns):
        assert columns == service.COLUMNS
        return self

    def eq(self, key, value):
        assert (key, value) == ('active', True)
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, start, end):
        self.bounds = (start, end + 1)
        self.calls.append(('range', start, end))
        return self

    def rpc(self, name, args):
        self.calls.append((name, args))
        self.rpc_args = (name, args)
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError('закрытые подробности')
        if self.rpc_args:
            name, args = self.rpc_args
            if name == 'set_carousel_author_hidden':
                username = args['p_username']
                row = self.rows.setdefault(username, {'username': username, 'hidden_at': DATE, 'active': False})
                row['active'] = args['p_hidden']
                return SimpleNamespace(data=row if row['active'] else {'username': username, 'hidden': False})
            assert name == 'import_carousel_hidden_authors'
            for author in args['p_authors']:
                self.rows.setdefault(author['username'], dict(author, active=True))
            authors = [row for row in self.rows.values() if row['active']]
            return SimpleNamespace(data={'authors': authors, 'total': len(authors)})
        rows = [row for row in self.rows.values() if row['active']]
        return SimpleNamespace(data=rows[slice(*self.bounds)])


@pytest.fixture
def setup(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(service, 'get_db', lambda: db)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    return TestClient(app), db


def test_hide_read_restore_and_tombstone_import(setup):
    client, db = setup
    for _ in range(2):
        response = client.put(URL + '/alice')
        assert response.json() == {'item': {'username': 'alice', 'hidden_at': DATE}}
        assert response.headers['cache-control'] == 'no-store'
    assert client.get(URL).json()['total'] == 1
    for name in ('alice', 'never_hidden'):
        assert client.delete(URL + '/' + name).json() == {'username': name, 'hidden': False}
    response = client.post(URL + '/import', json={'authors': [
        {'username': name, 'hidden_at': DATE} for name in ('alice', 'never_hidden', 'bob', 'bob')]})
    assert response.json() == {'authors': [{'username': 'bob', 'hidden_at': DATE}], 'total': 1}
    assert response.headers['cache-control'] == 'no-store'
    assert len(db.rows) == 3


def test_full_list_pagination(setup):
    client, db = setup
    db.rows = {str(i): {'username': str(i), 'hidden_at': DATE, 'active': True} for i in range(1600)}
    response = client.get(URL)
    assert response.json()['total'] == 1600
    assert ('range', 1000, 1999) in db.calls
    assert set(response.json()['authors'][0]) == {'username', 'hidden_at'}


@pytest.mark.parametrize('author', [
    {'username': 'Alice', 'hidden_at': DATE}, {'username': 'a'*31, 'hidden_at': DATE},
    {'username': 'a', 'hidden_at': 'infinity'}, {'username': 'a', 'hidden_at': '2026-10-20'},
    {'username': 'a', 'hidden_at': '2026-02-30T10:00:00Z'},
    {'username': 'a', 'hidden_at': DATE, 'active': True}, {'username': 123, 'hidden_at': DATE},
])
def test_invalid_batch_is_atomic_before_rpc(setup, author):
    client, db = setup
    response = client.post(URL + '/import', json={'authors': [{'username': 'valid', 'hidden_at': DATE}, author]})
    assert response.status_code == 422
    assert db.calls == []


def test_import_limit_and_strict_bodies(setup):
    client, db = setup
    for body in ({'authors': [], 'extra': 1}, {}, {'authors': [{'username': 'a', 'hidden_at': DATE}]*1601}):
        assert client.post(URL + '/import', json=body).status_code == 422
    for method in (client.put, client.delete):
        assert method(URL + '/Alice').status_code == 422
        assert client.request(method.__name__.upper(), URL + '/alice', content='{}').status_code == 422
    assert db.calls == []


def test_database_failures_are_sanitized(setup):
    client, db = setup
    db.fail = True
    for method, path, kwargs in [(client.get, URL, {}), (client.put, URL+'/a', {}),
                                 (client.delete, URL+'/a', {}),
                                 (client.post, URL+'/import', {'json': {'authors': []}})]:
        response = method(path, **kwargs)
        assert response.status_code == 503
        assert 'закрытые' not in response.text
