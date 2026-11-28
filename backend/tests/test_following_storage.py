from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.radar_discovery import router
from app.core.config import settings
from app.radar import following_storage as storage
from app.radar.following_discovery import DiscoveryError
from app.workers import following_discovery_sync as sync

_REF = "abcdefghijklmnopqrst"
_URL = f"https://{_REF}.supabase.co"
_ROUTE = "/api/radar/discovery"
_BUCKET = "reelscribe-discovery"
_OBJECT_PATH = f"/storage/v1/object/{_BUCKET}/snapshots/donor.json"


def _source(*, bio=None, status="partial", profile_checked_at=None, rows=True, day=28):
    return {"username": "donor", "status": status, "expected": 5, "collected": int(rows),
            "checked_at": f"2026-11-{day}T00:00:00Z", "reason": None,
            "rows": [{"username": "author", "bio": bio, "profile_checked_at": profile_checked_at}] if rows else []}


def _payload(**options):
    return json.dumps(_source(**options)).encode()


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", _URL)
    monkeypatch.delenv("FOLLOWING_DISCOVERY_STORAGE_BUCKET", raising=False)
    monkeypatch.delenv("FOLLOWING_DISCOVERY_DIR", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(settings, "supabase_service_role_key", "test-role-only")
    dot_temp = tmp_path / "supabase" / ".temp"
    dot_temp.mkdir(parents=True)
    (dot_temp / "project-ref").write_text(_REF)
    return tmp_path


def _client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1:5177", client=("127.0.0.1", 48000))


def _transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(storage.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_vercel_get_and_post_read_private_object_fresh_without_starting(target, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("FOLLOWING_DISCOVERY_LOCAL_START", "1")
    nonces = set()
    responses = [
        httpx.Response(200, content=_payload(bio="Первый", status="running")),
        httpx.Response(200, content=_payload(bio="После обновления")),
        httpx.Response(200, content=_payload(bio="После обновления")),
        httpx.Response(400, json={"statusCode": "404", "error": "not_found", "code": "NoSuchKey", "message": "Object not found"}),
    ]

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == f"{_REF}.supabase.co"
        assert request.url.path == _OBJECT_PATH or request.url.path.endswith("/new_donor.json")
        assert request.headers.get("Authorization") == "Bearer test-role-only"
        assert request.headers.get("apikey") == "test-role-only"
        assert request.headers.get("Cache-Control") == "no-cache, no-store"
        nonce = request.url.params["cacheNonce"]
        assert nonce not in nonces
        nonces.add(nonce)
        return responses.pop(0)

    _transport(monkeypatch, handler)
    with _client() as client:
        first = client.get(f"{_ROUTE}/donor")
        assert first.json()["status"] == "running"
        assert first.json()["rows"][0]["bio"] == "Первый"
        final = client.get(f"{_ROUTE}/donor")
        reused = client.post(_ROUTE, json={"input": "@DoNoR"})
        assert first.headers["Cache-Control"] == final.headers["Cache-Control"] == "no-store"
        assert final.json() == reused.json()
        assert final.json()["rows"][0]["bio"] == "После обновления"
        denied = client.post(_ROUTE, json={"input": "new_donor"},
                             headers={"Origin": "http://127.0.0.1:5177", "Sec-Fetch-Site": "same-origin"})
        assert denied.status_code == 403
        assert denied.headers["Cache-Control"] == "no-store"
    assert len(nonces) == 4


@pytest.mark.parametrize(("upstream", "expected"), [
    (httpx.Response(404, json={"code": "NoSuchKey"}), 404),
    (httpx.Response(404, json={"code": "NoSuchBucket"}), 503),
    (httpx.Response(401), 503),
    (httpx.Response(200, json=_source(status="running", rows=False)), 503),
])
def test_cloud_errors_do_not_cache_or_expose_credentials(target, monkeypatch, upstream, expected):
    monkeypatch.setenv("VERCEL", "1")
    _transport(monkeypatch, lambda _request: upstream)
    with _client() as client:
        response = client.get(f"{_ROUTE}/donor")
        assert response.status_code == expected
        assert response.headers["Cache-Control"] == "no-store"
        assert "test-role-only" not in response.text


def test_local_snapshot_has_priority(target, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("FOLLOWING_DISCOVERY_DIR", str(target))
    (target / "donor.json").write_bytes(_payload(bio="Локальный"))
    _transport(monkeypatch, lambda _: pytest.fail("Локальное чтение обратилось к Supabase"))
    with _client() as client:
        assert client.get(f"{_ROUTE}/donor").json()["rows"][0]["bio"] == "Локальный"
        assert client.post(_ROUTE, json={"input": "donor"}).json()["rows"][0]["bio"] == "Локальный"


def test_linked_cli_copies_only_projection_and_distinguishes_missing_from_network(target, monkeypatch):
    expected = f"ss:///{_BUCKET}/snapshots/donor.json"
    commands = []
    current = None
    next_failure = b''

    def execute(command, **kwargs):
        nonlocal current, next_failure
        commands.append(command)
        assert kwargs["timeout"] == 45
        assert kwargs["env"]["DO_NOT_TRACK"] == "1"
        assert command[1:3] == ["storage", "cp"]
        assert command[5:11] == ["--linked", "--experimental", "--workdir", str(target), "--output-format", "json"]
        source, destination = command[3:5]
        if next_failure:
            failure, next_failure = next_failure, b''
            return subprocess.CompletedProcess(command, 1, b'', failure)
        if source == expected and current is None:
            return subprocess.CompletedProcess(command, 1, b'{"error":{"message":"Object not found","code":"NoSuchKey"}}', b'')
        if source == expected:
            Path(destination).write_bytes(current)
        else:
            assert destination == expected
            assert command[-2:] == ["--content-type", "application/json"]
            current = Path(source).read_bytes()
            assert "private_checkpoint" not in current.decode()
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(sync.subprocess, "run", execute)
    path = target / "donor.json"
    path.write_bytes(json.dumps({**_source(), "private_checkpoint": "synthetic"}).encode())
    sync._run(path, target, "/example/supabase", watch=False, poll_seconds=5)
    assert len(commands) == 3
    assert storage.decode_snapshot(current, "donor").collected == 1
    next_failure = b'{"error":{"code":"NetworkError","message":"read timeout"}}'
    with pytest.raises(DiscoveryError, match="временно"):
        sync._run(path, target, "/example/supabase", watch=False, poll_seconds=5)
    assert len(commands) == 4
    sync._run(path, target, "/example/supabase", watch=False, poll_seconds=5)
    assert len(commands) == 5
    with pytest.raises(DiscoveryError, match="отличается"):
        monkeypatch.setenv("SUPABASE_URL", "https://other.supabase.co")
        sync._run(path, target, "/example/supabase", watch=False, poll_seconds=5)
    assert len(commands) == 5


def test_watcher_avoids_idle_requests_retries_latest_and_rejects_rollback(target, monkeypatch):
    path = target / "donor.json"
    old = _payload(bio="Первый")
    new = _payload(bio="Новый", profile_checked_at="2026-11-29")
    path.write_bytes(old)

    class MockStorage:
        def __init__(self):
            self.raw = None
            self.reads = 0
            self.uploads = []

        def read(self, username):
            self.reads += 1
            if self.reads == 1:
                raise DiscoveryError(503, "Облачное хранилище временно недоступно.")
            if self.raw:
                return sync.Published(storage.decode_snapshot(self.raw, username), sync._digest(self.raw))
            return None

        def upload(self, username, content):
            self.raw = content
            self.uploads.append(username)

    remote = MockStorage()
    monkeypatch.setattr(sync, "LinkedStorage", lambda *_: remote)
    checks = 0

    def wait(seconds):
        nonlocal checks
        checks += 1
        if checks == 1:
            path.write_bytes(new)
        elif checks == 2:
            assert remote.reads == 3
        elif checks == 3:
            assert remote.reads == 3
            path.write_bytes(old)
        elif checks == 4:
            assert remote.reads == 4
            raise KeyboardInterrupt

    monkeypatch.setattr(sync.time, "sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        sync._run(path, target, "/example/supabase", watch=True, poll_seconds=5)
    assert remote.uploads == ["donor"]
    assert storage.decode_snapshot(remote.raw, "donor").rows[0].bio == "Новый"


def test_new_enrichment_progress_can_publish_but_old_running_cannot_replace_final():
    final = storage.decode_snapshot(_payload(profile_checked_at="2026-11-29"), "donor")
    assert sync._is_older(storage.decode_snapshot(_payload(status="running"), "donor"), final)
    assert sync._is_older(storage.decode_snapshot(_payload(status="running", profile_checked_at="2026-11-29"), "donor"), final)
    newer = storage.decode_snapshot(_payload(status="running", profile_checked_at="2026-11-30"), "donor")
    assert sync._is_older(newer, final) is False
