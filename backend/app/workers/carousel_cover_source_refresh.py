"""Одноразовое ограниченное обновление источников; на диск выходит лишь шифротекст."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.radar.activity_job import LIMIT_CODES, USERNAME, _usage

REPO = 'ilfaaat-sistema/reelscribe'
API_BASE = 'https://reelscribe-ai.vercel.app/api'
GRAPH_BASE = 'https://graph.facebook.com/v23.0'
MAX_REQUESTS = 30
MAX_AUTHOR_PAGES = 3
RUN_SECONDS = 25 * 60
MEDIA_FIELDS = 'id,media_type,media_url,thumbnail_url,timestamp,permalink'
# Тот же allowlist, что у carousel_cover_image.download_image; без тяжёлого импорта API.
IMAGE_HOSTS = ('cdninstagram.com', 'fbcdn.net', 'instagram.com')
CURSOR = re.compile(r'[A-Za-z0-9_\-=/+]{1,2048}')


class SafeFailure(Exception):
    """Исключение содержит только фиксированный служебный код."""


def parse_ids(value):
    if not isinstance(value, str) or len(value) > 820:
        raise SafeFailure('invalid_ids')
    ids = value.split(',')
    if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids) or any(
            not re.fullmatch(r'[0-9]{1,40}', item) for item in ids):
        raise SafeFailure('invalid_ids')
    return ids


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def recipient_key(pem):
    try:
        if not isinstance(pem, str) or len(pem) > 16384:
            raise ValueError()
        key = serialization.load_pem_public_key(pem.encode('ascii'))
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError()
        return key
    except (ValueError, TypeError, UnicodeError):
        raise SafeFailure('invalid_recipient_key') from None


def source_url(value, now):
    """Возвращает только безопасный URL, ограниченную дату и основание срока."""
    try:
        if not isinstance(value, str) or not value or len(value) > 8192:
            return None
        decoded = unquote(value)
        if any(ord(c) <= 32 or ord(c) == 127 for c in decoded):
            return None
        if 'access_token' in decoded.lower() or 'bearer' in decoded.lower() or '\\' in decoded:
            return None
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                or url.fragment or not any(host == h or host.endswith('.' + h) for h in IMAGE_HOSTS)):
            return None
        query = parse_qs(url.query, keep_blank_values=True)
        if 'oe' in query:
            if len(query['oe']) != 1 or not re.fullmatch(r'[0-9a-fA-F]{1,16}', query['oe'][0]):
                return None
            expiry = datetime.fromtimestamp(int(query['oe'][0], 16), timezone.utc)
            if expiry <= now:
                return None
            return value, min(expiry, now + timedelta(hours=24)), 'oe_capped_24h'
        return value, now + timedelta(hours=6), 'fallback_6h'
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def json_response(response):
    if len(response.content) > 4 * 1024 * 1024:
        raise SafeFailure('response_too_large')
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, TypeError):
        raise SafeFailure('invalid_response') from None


def production_get(client, path, params=None):
    try:
        response = client.get(API_BASE + path, params=params, timeout=45, follow_redirects=False)
    except httpx.TransportError:
        raise SafeFailure('production_transport_error') from None
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SafeFailure('production_http_error')
    return json_response(response)


def eligible_groups(client, ids, can_continue=lambda: True):
    if not can_continue():
        raise SafeFailure('refresh_deadline')
    cached = production_get(client, '/radar/carousel-cover-translations', {'ids': ','.join(ids)})
    rows = cached.get('items') if cached else None
    if (not isinstance(rows, list) or len(rows) != len(ids)
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows)):
        raise SafeFailure('invalid_cover_cache')
    by_id = {row.get('id'): row for row in rows}
    if len(by_id) != len(ids) or set(by_id) != set(ids):
        raise SafeFailure('invalid_cover_cache')
    groups, rejected = defaultdict(set), {}
    for identity in ids:
        row = by_id[identity]
        # Отсутствующий признак не считается подтверждённым отсутствием claim.
        if row.get('status') != 'image_unavailable' or row.get('attempt_recorded') is not False:
            rejected[identity] = 'ineligible_cache'
            continue
        detail = None
        for period in ('30d', '14d', 'archive14d', '7d'):
            if not can_continue():
                raise SafeFailure('refresh_deadline')
            detail = production_get(client, '/radar/carousels/' + identity, {'period': period})
            if detail is not None:
                break
        if (not detail or detail.get('id') != identity or detail.get('media_type') != 'CAROUSEL_ALBUM'
                or not isinstance(detail.get('username'), str)
                or not USERNAME.fullmatch(detail['username'])):
            rejected[identity] = 'catalogue_not_found'
            continue
        groups[detail['username'].lower()].add(identity)
    return groups, rejected


def refresh(client, ids, *, token, account_id, run_id, commit, now=None, monotonic=None):
    """Ноль повторов; общий счётчик увеличивается до каждого Meta GET."""
    ids = parse_ids(','.join(ids))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or not token or not re.fullmatch(r'[0-9]{1,40}', account_id):
        raise SafeFailure('invalid_runtime_configuration')
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + RUN_SECONDS
    groups, rejected = eligible_groups(client, ids, lambda: monotonic() < deadline)
    items = {identity: {'id': identity, 'refreshed_at': now.isoformat(), 'expires_at': now.isoformat(),
                       'provenance': 'meta_graph_refresh', 'status': 'not_found',
                       'safe_error_code': rejected.get(identity, 'not_found_within_bounds')}
             for identity in ids}
    requests, stop = 0, None
    for username, targets in groups.items():
        cursor, seen = None, set()
        for _ in range(MAX_AUTHOR_PAGES):
            if monotonic() >= deadline:
                stop = 'refresh_deadline'
                break
            if requests >= MAX_REQUESTS:
                stop = 'request_cap'
                break
            media = 'media.limit(50)' + (f'.after({cursor})' if cursor else '')
            fields = f'business_discovery.username({username}){{{media}{{{MEDIA_FIELDS}}}}}'
            requests += 1
            try:
                response = client.get(GRAPH_BASE + '/' + account_id,
                                      headers={'Authorization': 'Bearer ' + token},
                                      params={'fields': fields}, timeout=45, follow_redirects=False)
                payload = json_response(response)
            except httpx.TransportError:
                stop = 'meta_transport_error'
                break
            except SafeFailure as exc:
                stop = str(exc)
                break
            error = payload.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            code = code if type(code) is int else None
            usage = max(_usage(response.headers).values(), default=0)
            if response.status_code in (401, 403) or code in (190, 10, 200, 294):
                stop = 'meta_auth_or_permissions'
                break
            if response.status_code == 429 or code in LIMIT_CODES:
                stop = 'meta_rate_limit'
                break
            if response.status_code != 200 or error:
                stop = 'meta_rate_limit' if usage >= 85 else 'meta_http_error'
                break
            discovery = payload.get('business_discovery')
            page = discovery.get('media') if isinstance(discovery, dict) else None
            batch = page.get('data') if isinstance(page, dict) else None
            if not isinstance(batch, list) or len(batch) > 50:
                stop = 'invalid_meta_response'
                break
            for raw in batch:
                if (not isinstance(raw, dict) or not isinstance(raw.get('id'), str)
                        or raw['id'] not in targets):
                    continue
                identity = raw['id']
                targets.remove(identity)
                row = items[identity]
                if raw.get('media_type') != 'CAROUSEL_ALBUM':
                    row.update(status='unavailable', safe_error_code='media_type_mismatch')
                    continue
                sources = {name: valid for name in ('media_url', 'thumbnail_url')
                           if (valid := source_url(raw.get(name), now))}
                if not sources:
                    row.update(status='unavailable', safe_error_code='unsafe_or_expired_source')
                    continue
                row.update({name: value[0] for name, value in sources.items()})
                row.update(status='refreshed', safe_error_code=None,
                           expires_at=min(value[1] for value in sources.values()).isoformat(),
                           declared_expiry_basis={name: value[2] for name, value in sources.items()})
            if usage >= 85:
                stop = 'meta_rate_limit'
                break
            if not targets:
                break
            paging = page.get('paging')
            cursors = paging.get('cursors') if isinstance(paging, dict) else None
            after = cursors.get('after') if isinstance(cursors, dict) else None
            if not after or not batch:
                break
            if not isinstance(after, str) or not CURSOR.fullmatch(after) or after in seen:
                stop = 'invalid_or_repeated_cursor'
                break
            seen.add(after)
            cursor = after
        if stop:
            break
    if stop:
        for row in items.values():
            if row['status'] == 'not_found' and row['id'] not in rejected:
                row.update(status='partial', safe_error_code=stop)
    return {'version': 1, 'repo': REPO, 'run_id': run_id, 'commit': commit,
            'requested_ids': ids, 'request_count': requests, 'items': list(items.values())}


def seal(result, key):
    fingerprint = hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    metadata = {name: result[name] for name in ('version', 'repo', 'run_id', 'commit')}
    metadata.update(requested_ids_sha256=hashlib.sha256(canonical(sorted(result['requested_ids']))).hexdigest(),
                    recipient_key_sha256=fingerprint, cipher='AES-256-GCM', key_wrap='RSA-OAEP-SHA256')
    content_key, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(nonce, canonical(result), canonical(metadata))
    wrapped = key.encrypt(content_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                   algorithm=hashes.SHA256(), label=None))
    return {'metadata': metadata, 'wrapped_key': base64.b64encode(wrapped).decode(),
            'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(ciphertext).decode()}


def write_sealed(path, envelope):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(canonical(envelope))


def main():
    try:
        if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_REF') != 'refs/heads/main'
                or os.environ.get('GITHUB_REPOSITORY') != REPO):
            raise SafeFailure('invalid_workflow_context')
        ids = parse_ids(os.environ.get('COVER_REFRESH_IDS'))
        key = recipient_key(os.environ.get('COVER_REFRESH_RECIPIENT_KEY'))
        run_id, commit = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_SHA', '')
        if not re.fullmatch(r'[0-9]+', run_id) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise SafeFailure('invalid_workflow_provenance')
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), follow_redirects=False,
                          trust_env=False, timeout=45) as client:
            result = refresh(client, ids, token=os.environ.get('META_IG_TOKEN'),
                             account_id=os.environ.get('META_IG_USER_ID', ''), run_id=run_id, commit=commit)
        write_sealed(Path(os.environ['RUNNER_TEMP']) / 'carousel-cover-source-refresh' / 'sealed.json',
                     seal(result, key))
        print(json.dumps({'status': 'sealed', 'request_count': result['request_count'],
                          'requested': len(ids), 'refreshed': sum(r['status'] == 'refreshed' for r in result['items'])}))
        return 0
    except Exception:
        # Никаких repr/traceback: исключение HTTP может содержать URL или credentials.
        print('{"status":"source_refresh_failed"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
