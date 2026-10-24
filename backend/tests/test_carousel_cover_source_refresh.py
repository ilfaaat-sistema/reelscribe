"""Контракт ограниченного refresh и шифрования без реальных сетевых запросов."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.workers import carousel_cover_source_refresh as worker

NOW = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
URL = 'https://scontent.cdninstagram.com/image.jpg?oe=' + format(int((NOW + timedelta(hours=10)).timestamp()), 'x')


def meta_page(rows=(), cursor=None):
    return {'business_discovery': {'media': {'data': list(rows),
            'paging': {'cursors': {'after': cursor}, 'next': 'https://evil.invalid/?access_token=secret'}}}}


def media(identity, **extra):
    return {'id': identity, 'media_type': 'CAROUSEL_ALBUM', 'media_url': URL,
            'caption': 'чужой текст не сохранять', **extra}


class Network:
    def __init__(self, pages, ids=('1',), usernames=None, cache=None):
        self.pages = iter(pages)
        self.ids, self.usernames, self.cache = ids, usernames or {}, cache
        self.calls, self.meta_calls = [], []

    def handler(self, request):
        self.calls.append(request)
        if request.url.host == 'reelscribe-ai.vercel.app':
            assert 'authorization' not in request.headers
            if request.url.path.endswith('carousel-cover-translations'):
                return httpx.Response(200, json={'items': self.cache or [
                    {'id': identity, 'status': 'image_unavailable', 'attempt_recorded': False}
                    for identity in self.ids]})
            identity = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={'id': identity, 'media_type': 'CAROUSEL_ALBUM',
                'username': self.usernames.get(identity, 'known.author'), 'caption': 'не сохранять'})
        assert request.url.host == 'graph.facebook.com'
        assert request.url.path == '/v23.0/999'
        assert request.headers['authorization'] == 'Bearer test-secret'
        assert 'access_token' not in str(request.url)
        assert 'caption' not in request.url.params['fields']
        self.meta_calls.append(request)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, tuple):
            return httpx.Response(page[0], json=page[1], headers=page[2] if len(page) > 2 else {})
        return httpx.Response(200, json=page)

    def run(self):
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            return worker.refresh(client, list(self.ids), token='test-secret', account_id='999',
                                  run_id='123', commit='a' * 40, now=NOW)


@pytest.mark.parametrize('value', ['', '1,', ',1', '1,1', ' 1', '1,2x', '１', ','.join(str(i) for i in range(21)), '9' * 41])
def test_id_validation(value):
    with pytest.raises(worker.SafeFailure, match='invalid_ids'):
        worker.parse_ids(value)


def test_grouping_pagination_matching_only_and_stop_after_found():
    network = Network([meta_page([media('unrelated')], 'cursor1'),
                       meta_page([media('1'), media('2'), media('unrelated')], 'never-follow')], ids=('1', '2'))
    result = network.run()
    assert result['request_count'] == 2
    assert len(network.meta_calls) == 2
    assert '.after(cursor1)' in network.meta_calls[1].url.params['fields']
    assert len(result['items']) == 2
    assert all(row['status'] == 'refreshed' for row in result['items'])
    assert not any(word in json.dumps(result) for word in ('caption', 'unrelated', 'paging', 'test-secret'))


def test_auth_does_not_cross_to_production_and_redirect_is_not_followed():
    network = Network([(302, {'location': 'https://evil.invalid'})])
    result = network.run()
    assert len(network.calls) == 3
    assert result['items'][0]['safe_error_code'] == 'meta_http_error'


def test_only_confirmed_unclaimed_image_unavailable_is_eligible():
    cache = [{'id': '1', 'status': 'ready', 'attempt_recorded': False},
             {'id': '2', 'status': 'image_unavailable', 'attempt_recorded': True},
             {'id': '3', 'status': 'image_unavailable'}]
    network = Network([], ids=('1', '2', '3'), cache=cache)
    result = network.run()
    assert len(network.calls) == 1
    assert result['request_count'] == 0
    assert all(row['safe_error_code'] == 'ineligible_cache' for row in result['items'])


def test_catalogue_period_fallback_and_invalid_author():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('carousel-cover-translations'):
            return httpx.Response(200, json={'items': [{'id': '1', 'status': 'image_unavailable', 'attempt_recorded': False}]})
        if request.url.params['period'] != 'archive14d':
            return httpx.Response(404)
        return httpx.Response(200, json={'id': '1', 'media_type': 'CAROUSEL_ALBUM', 'username': 'x){injection'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = worker.refresh(client, ['1'], token='test-secret', account_id='999', run_id='123', commit='a'*40, now=NOW)
    assert len(calls) == 4
    assert result['request_count'] == 0
    assert result['items'][0]['safe_error_code'] == 'catalogue_not_found'


def test_caps_three_pages_per_author_and_thirty_total():
    ids = tuple(str(i) for i in range(12))
    network = Network([meta_page([media('unrelated')], f'cursor{i}') for i in range(30)],
                      ids=ids, usernames={identity: 'author' + identity for identity in ids})
    result = network.run()
    assert result['request_count'] == len(network.meta_calls) == 30
    assert len({r.url.params['fields'].split(')')[0] for r in network.meta_calls}) == 10
    assert all(row['safe_error_code'] == 'request_cap' for row in result['items'])


@pytest.mark.parametrize('response,code', [
    ((403, {'error': {'code': 100, 'message': URL}}), 'meta_auth_or_permissions'),
    ((200, {'error': {'code': 190, 'message': 'test-secret'}}), 'meta_auth_or_permissions'),
    ((429, {'error': {'code': 4}}), 'meta_rate_limit'),
    ((200, {'error': {'code': 613}}), 'meta_rate_limit'),
    ((200, meta_page(), {'x-app-usage': '{"call_count":85}'}), 'meta_rate_limit'),
    (httpx.ConnectError('test-secret ' + URL), 'meta_transport_error'),
])
def test_global_stop_partial_no_retry(response, code):
    network = Network([meta_page([media('1')]), response], ids=('1', '2', '3'),
                      usernames={'1': 'first', '2': 'second', '3': 'third'})
    result = network.run()
    assert result['request_count'] == len(network.meta_calls) == 2
    assert result['items'][0]['status'] == 'refreshed'
    assert all(row['safe_error_code'] == code for row in result['items'][1:])
    assert 'test-secret' not in json.dumps(result)


@pytest.mark.parametrize('value', ['http://cdninstagram.com/a', 'https://cdninstagram.com.evil/a',
    'https://127.0.0.1/a', 'https://cdninstagram.com:444/a', 'https://u@cdninstagram.com/a',
    'https://cdninstagram.com/a#secret', 'https://cdninstagram.com/a?access_token=x',
    'https://cdninstagram.com/a?x=%42earerX', 'https://cdninstagram.com/a?oe=0',
    'https://cdninstagram.com/a?oe=invalid', 'https://cdninstagram.com/a?oe=',
    'https://cdninstagram.com/a?oe=ffffffffffffffff', 'https://cdninstagram.com/%0afoo',
    'https://cdninstagram.com/' + 'x' * 8192])
def test_unsafe_sources_rejected(value):
    assert worker.source_url(value, NOW) is None


def test_expiry_limits_and_thumbnail_only():
    long = 'https://s.fbcdn.net/a?oe=' + format(int((NOW + timedelta(days=8)).timestamp()), 'x')
    assert worker.source_url(long, NOW)[1:] == (NOW + timedelta(hours=24), 'oe_capped_24h')
    assert worker.source_url('https://instagram.com/a', NOW)[1:] == (NOW + timedelta(hours=6), 'fallback_6h')
    network = Network([meta_page([media('1', media_url='https://evil.invalid', thumbnail_url=URL)])])
    row = network.run()['items'][0]
    assert 'media_url' not in row and row['thumbnail_url'] == URL
    assert row['expires_at'] == (NOW + timedelta(hours=10)).isoformat()
    assert row['declared_expiry_basis'] == {'thumbnail_url': 'oe_capped_24h'}


def test_usage_stop_preserves_current_matching_page():
    network = Network([(200, meta_page([media('1')]), {'x-app-usage': '{"call_count":90}'})],
                      ids=('1', '2'), usernames={'1': 'first', '2': 'second'})
    result = network.run()
    assert result['request_count'] == 1
    assert result['items'][0]['status'] == 'refreshed'
    assert result['items'][1]['safe_error_code'] == 'meta_rate_limit'


def test_runtime_deadline_leaves_time_to_seal_partial():
    network = Network([], ids=('1',))
    ticks = iter([0, 0, 0, worker.RUN_SECONDS + 1])
    with httpx.Client(transport=httpx.MockTransport(network.handler)) as client:
        result = worker.refresh(client, ['1'], token='test-secret', account_id='999', run_id='123',
                                commit='a'*40, now=NOW, monotonic=lambda: next(ticks))
    assert result['request_count'] == 0
    assert result['items'][0]['safe_error_code'] == 'refresh_deadline'


@pytest.mark.parametrize('cursor', ['x){caption}', 'https://evil.invalid', ['invalid']])
def test_untrusted_cursor_not_used(cursor):
    network = Network([meta_page([media('unrelated')], cursor)])
    result = network.run()
    assert result['request_count'] == 1
    assert result['items'][0]['safe_error_code'] == 'invalid_or_repeated_cursor'


def test_unsafe_matching_source_does_not_expose_urls():
    network = Network([meta_page([media('1', media_url='https://evil.invalid/private',
                                        thumbnail_url='https://cdninstagram.com/a?access_token=secret')])])
    row = network.run()['items'][0]
    assert row['safe_error_code'] == 'unsafe_or_expired_source'
    assert 'media_url' not in row and 'thumbnail_url' not in row


@pytest.fixture(scope='module')
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def decrypt(envelope, private_key):
    key = private_key.decrypt(base64.b64decode(envelope['wrapped_key']),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return AESGCM(key).decrypt(base64.b64decode(envelope['nonce']), base64.b64decode(envelope['ciphertext']),
                               worker.canonical(envelope['metadata']))


def test_seal_roundtrip_aad_and_key_fingerprint_tamper(private_key):
    result = Network([meta_page([media('1')])]).run()
    envelope = worker.seal(result, private_key.public_key())
    assert json.loads(decrypt(envelope, private_key)) == result
    assert envelope != worker.seal(result, private_key.public_key())
    for field in ('repo', 'run_id', 'commit', 'requested_ids_sha256', 'recipient_key_sha256', 'version'):
        changed = json.loads(json.dumps(envelope))
        changed['metadata'][field] = 'tampered'
        with pytest.raises(InvalidTag):
            decrypt(changed, private_key)
    changed = json.loads(json.dumps(envelope))
    data = bytearray(base64.b64decode(changed['ciphertext']))
    data[0] ^= 1
    changed['ciphertext'] = base64.b64encode(data).decode()
    with pytest.raises(InvalidTag):
        decrypt(changed, private_key)


def test_recipient_key_rejects_private_and_small_keys(private_key):
    pem = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    assert worker.recipient_key(pem).key_size == 2048
    for value in ('bad', private_key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                  rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key().public_bytes(
                  serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()):
        with pytest.raises(worker.SafeFailure):
            worker.recipient_key(value)


def test_main_only_ciphertext_file_and_safe_logs(tmp_path, monkeypatch, capsys, private_key):
    result = Network([meta_page([media('1')])]).run()
    pem = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    for name, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_REPOSITORY': worker.REPO, 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'a'*40,
        'COVER_REFRESH_IDS': '1', 'COVER_REFRESH_RECIPIENT_KEY': pem, 'RUNNER_TEMP': str(tmp_path)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(worker, 'refresh', lambda *a, **kw: result)
    assert worker.main() == 0
    files = [path for path in tmp_path.rglob('*') if path.is_file()]
    assert [path.name for path in files] == ['sealed.json']
    assert files[0].stat().st_mode & 0o777 == 0o600
    raw = files[0].read_text()
    logs = capsys.readouterr().out
    assert not any(value in raw + logs for value in (URL, 'test-secret', 'media_url', 'caption'))
    assert json.loads(decrypt(json.loads(raw), private_key)) == result
    monkeypatch.setattr(worker, 'refresh', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(URL + ' test-secret')))
    assert worker.main() == 1
    assert capsys.readouterr().out == '{"status":"source_refresh_failed"}\n'


def test_workflow_safe_contract():
    path = Path(__file__).parents[2] / '.github/workflows/carousel-cover-source-refresh.yml'
    text = path.read_text()
    assert 'contents: read' in text and 'contents: write' not in text and 'actions: write' not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert 'retention-days: 1' in text and 'cryptography==50.0.2' in text
    assert 'path: ${{ runner.temp }}/carousel-cover-source-refresh/sealed.json' in text
    assert 'upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02' in text
    assert '*' not in text and 'schedule:' not in text and 'repository_dispatch:' not in text
