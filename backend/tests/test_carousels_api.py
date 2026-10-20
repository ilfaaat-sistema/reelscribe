from app.api.carousels import page


def report():
    base = {'media_type': 'CAROUSEL_ALBUM', 'username': 'alice', 'timestamp': '2026-10-15T10:00:00+00:00'}
    return {'start_date': '2026-10-13', 'end_date': '2026-10-19', 'collection': {'checked': 1},
            'items': [dict(base, id='1', likes=10, comments=1), dict(base, id='2', likes=None),
                      dict(base, id='3', likes=100, comments=2), dict(base, id='3', likes=100),
                      dict(base, id='4', likes=1000, username='bob')]}


def test_filters_before_pagination_and_nulls_last_in_both_orders():
    value = report()
    assert [i['id'] for i in page(value, 'alice', limit=2)['items']] == ['3', '1']
    assert page(value, 'alice', limit=2)['total'] == 3
    assert [i['id'] for i in page(value, 'alice', order='asc')['items']] == ['1', '3', '2']
    assert page(value, 'alice', offset=2)['items'][0]['likes'] is None


def test_untrusted_urls_and_unapproved_fields_are_never_returned():
    value = report()
    value['items'][0].update(media_url='http://127.0.0.1/private', permalink='https://evil.test/',
                             caption='Обычный текст', access_token='test-forbidden-field', likes=True)
    item = next(i for i in page(value)['items'] if i['id'] == '1')
    assert item['media_url'] is None and item['permalink'] is None and item['likes'] is None
    assert 'access_token' not in item
    assert item['caption'] == 'Обычный текст'


def test_unknown_author_empty_and_collection_status_preserved():
    result = page(report(), 'not_collected')
    assert result['total'] == 0 and result['items'] == []
    assert result['collection']['checked'] == 1


def test_http_interface_validates_filters_and_detail_ids(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import carousels
    async def data(period=None):
        return report()
    monkeypatch.setattr(carousels, '_report', data)
    app = FastAPI()
    app.include_router(carousels.router, prefix='/api')
    client = TestClient(app)
    response = client.get('/api/radar/carousels?author=alice&limit=50&sort=likes')
    assert response.status_code == 200 and response.json()['total'] == 3
    assert client.get('/api/radar/carousels?limit=75').status_code == 422
    assert client.get('/api/radar/carousels?sort=views').status_code == 422
    assert client.get('/api/radar/carousels/../../private').status_code == 404
    assert client.get('/api/radar/carousels/1').json()['id'] == '1'


def test_exclusions_apply_before_sorting_counting_and_page_fill():
    value = report()
    result = page(value, limit=2, exclude_authors=['@ALICE'])
    assert result['total'] == 1
    assert [row['username'] for row in result['items']] == ['bob']
    assert page(value, 'alice', exclude_authors=['alice'])['total'] == 0
    assert value['items'][0]['username'] == 'alice'
    assert page(value)['total'] == 4


def test_favorites_include_before_pagination_and_combine_with_exclusions():
    value = report()
    assert [row['id'] for row in page(value, include_authors=['@ALICE'])['items']] == ['3', '1', '2']
    assert page(value, include_authors=['alice'])['total'] == 3
    assert page(value, include_authors=[])['total'] == 0
    assert page(value, include_authors=['alice'], exclude_authors=['ALICE'])['total'] == 0
    assert page(value, include_authors=['bob'], author='alice')['total'] == 0


def test_hidden_authors_query_is_read_only_and_validated(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import carousels
    value = report()
    async def data(period=None):
        return value
    monkeypatch.setattr(carousels, '_report', data)
    app = FastAPI()
    app.include_router(carousels.router, prefix='/api')
    client = TestClient(app)
    response = client.post('/api/radar/carousels', json={'exclude_authors': ['ALICE', 'alice'], 'limit': 50})
    assert response.status_code == 200 and response.json()['total'] == 1
    assert response.json()['items'][0]['username'] == 'bob'
    assert client.get('/api/radar/carousels?limit=50').json()['total'] == 4
    assert client.post('/api/radar/carousels', json={'exclude_authors': ['evil/x']}).status_code == 422
    assert client.post('/api/radar/carousels', json={'exclude_authors': ['alice'] * 1601}).status_code == 422
    favorites = client.post('/api/radar/carousels', json={'include_authors': ['alice', '@ALICE']})
    assert favorites.status_code == 200 and favorites.json()['total'] == 3
    assert client.post('/api/radar/carousels', json={'include_authors': []}).json()['total'] == 0
    assert client.post('/api/radar/carousels', json={'include_authors': ['bad/name']}).status_code == 422


def test_source_account_filters_before_pagination_and_intersects_favorites(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import carousels
    async def data(period=None):
        return report()
    async def authors():
        return {'alice'}
    monkeypatch.setattr(carousels, '_report', data)
    monkeypatch.setattr(carousels, '_neyro_source_authors', authors)
    app = FastAPI()
    app.include_router(carousels.router, prefix='/api')
    client = TestClient(app)
    url = '/api/radar/carousels'
    result = client.post(url, json={'source_account': 'neyro_set7', 'limit': 50}).json()
    assert result['total'] == 3
    assert all(item['username'] == 'alice' for item in result['items'])
    assert client.post(url, json={'source_account': 'neyro_set7', 'include_authors': ['bob']}).json()['total'] == 0
    assert client.post(url, json={'source_account': 'neyro_set7', 'exclude_authors': ['alice']}).json()['total'] == 0
    assert client.get(url + '?limit=50').json()['total'] == 4
    assert client.post(url, json={'source_account': 'unknown'}).status_code == 422


def test_period_selection_dates_detail_and_filters(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from app.api import carousels
    available = False
    calls = []

    async def data(period='7d'):
        calls.append(period)
        if period == '14d' and not available:
            raise HTTPException(503, 'Сбор за 14 дней ещё не опубликован')
        value = report()
        if period == '14d':
            value['start_date'] = '2026-10-06'
            value['collection']['target'] = 586
            value['items'][0]['timestamp'] = '2026-10-06T00:01:00+03:00'
        return value

    monkeypatch.setattr(carousels, '_report', data)
    app = FastAPI()
    app.include_router(carousels.router)
    client = TestClient(app)
    url = '/radar/carousels'
    assert client.get(url).json()['start_date'] == '2026-10-13'
    assert calls == ['14d', '7d']
    assert client.get(url + '?period=').json()['start_date'] == '2026-10-13'
    assert client.get(url + '?period=archive14d').status_code == 503
    assert client.get(url + '/1?period=archive14d').status_code == 503
    available = True
    assert client.get(url).json()['start_date'] == '2026-10-06'
    assert client.get(url + '?period=7d').json()['start_date'] == '2026-10-13'
    detail = client.get(url + '/1?period=archive14d').json()
    assert detail['start_date'] == '2026-10-06'
    assert detail['end_date'] == '2026-10-19'
    assert detail['timestamp'] == '2026-10-06T00:01:00+03:00'
    filtered = client.post(url, json={'period': 'archive14d', 'include_authors': ['alice'], 'exclude_authors': ['bob']}).json()
    assert filtered['total'] == 3 and filtered['collection']['target'] == 586
    assert filtered['start_date'] == '2026-10-06'
    assert client.get(url + '?period=30d').status_code == 422
    assert client.get(url + '/1?period=30d').status_code == 422
    assert client.post(url, json={'period': '30d'}).status_code == 422


def test_old_detail_link_without_period_falls_back_to_week(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import carousels

    async def data(period='7d'):
        value = report()
        if period == '14d':
            value['start_date'] = '2026-10-06'
            value['collection']['target'] = 586
            value['items'] = [item for item in value['items'] if item['id'] != '4']
        return value

    monkeypatch.setattr(carousels, '_report', data)
    app = FastAPI()
    app.include_router(carousels.router)
    client = TestClient(app)
    assert client.get('/radar/carousels/4').json()['start_date'] == '2026-10-13'
    assert client.get('/radar/carousels/4?period=archive14d').status_code == 404


def test_live_period_is_separate_from_archived_report(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import carousels

    seen = []
    def live_page(**kwargs):
        seen.append(kwargs)
        return {'start_date': '2026-10-07', 'end_date': '2026-10-20', 'items': [], 'total': 0}
    def live_detail(media_id):
        return {'id': media_id, 'start_date': '2026-10-07'}
    monkeypatch.setattr(carousels.carousel_favorites, 'live_page', live_page)
    monkeypatch.setattr(carousels.carousel_favorites, 'live_detail', live_detail)
    app = FastAPI()
    app.include_router(carousels.router)
    client = TestClient(app)
    response = client.post('/radar/carousels', json={'period': '14d', 'include_authors': ['alice']})
    assert response.status_code == 200 and response.json()['start_date'] == '2026-10-07'
    assert seen[0]['include_authors'] == ['alice']
    assert client.get('/radar/carousels/1?period=14d').json()['id'] == '1'


def test_reports_use_isolated_urls_and_caches_and_validate_dates(monkeypatch):
    import asyncio
    import httpx
    from fastapi import HTTPException
    from app.api import carousels
    requests = []
    invalid = False

    def respond(request):
        requests.append(str(request.url))
        value = report()
        value['provider'] = 'meta_graph'
        if '20261006' in str(request.url):
            value['start_date'] = '2026-10-13' if invalid else '2026-10-06'
            value['collection']['target'] = 586
        return httpx.Response(200, json=value)

    original = httpx.AsyncClient
    monkeypatch.setattr(carousels.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(carousels, '_cache', {})
    monkeypatch.setattr(carousels, '_cached_at', {})
    async def check():
        assert (await carousels._report('14d'))['start_date'] == '2026-10-06'
        assert (await carousels._report('7d'))['start_date'] == '2026-10-13'
        await carousels._report('14d')
        assert len(requests) == 2
        assert '20261006-20261019' in requests[0]
        assert '20261013-20261019' in requests[1]
        carousels._cache.clear()
        nonlocal invalid
        invalid = True
        try:
            await carousels._report('14d')
        except HTTPException as error:
            assert error.status_code == 503
        else:
            raise AssertionError('Неверные даты приняты')
    asyncio.run(check())


def test_rolling_off_sources_and_detail_window_and_archive_failure(monkeypatch):
    from datetime import datetime, timezone
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from app.api import carousels
    api = carousels.carousel_favorites
    stamp = datetime.now(timezone.utc).isoformat()
    calls = []
    failed = False
    async def reports(period):
        calls.append(period)
        if failed:
            raise HTTPException(503, 'Архив временно недоступен')
        return {'items': [{'id': '123', 'username': 'other', 'media_type': 'CAROUSEL_ALBUM', 'timestamp': stamp},
                          {'id': '124', 'username': 'other', 'media_type': 'CAROUSEL_ALBUM', 'timestamp': '2000-01-01T00:00:00Z'}]}
    class FakeStore:
        def read_state(self):
            return {'authors': [], 'items': []}
    monkeypatch.setattr(api, '_store', lambda client: FakeStore())
    monkeypatch.setattr(carousels, '_report', reports)
    app = FastAPI()
    app.include_router(carousels.router)
    client = TestClient(app)
    url = '/radar/carousels'
    assert client.get(url + '?period=14d').json()['total'] == 0
    assert calls == []
    off = client.post(url, json={'period': '14d', 'favorites_only': False}).json()
    assert off['total'] == 1 and off['items'][0]['id'] == '123'
    assert client.get(url + '?period=14d&favorites_only=false').json()['total'] == 1
    detail = client.get(url + '/123?period=14d')
    assert detail.status_code == 200 and detail.json()['start_date'] == off['start_date']
    assert client.get(url + '/124?period=14d').status_code == 404
    failed = True
    assert client.post(url, json={'period': '14d', 'favorites_only': False}).status_code == 503
