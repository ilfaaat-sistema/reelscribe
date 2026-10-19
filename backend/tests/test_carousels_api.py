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
    async def data():
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
