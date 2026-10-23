"""Проверки GitHub CAS-хранилища без сети и учётных данных."""
import base64
import json

import httpx
import pytest
from app.services.carousel_cover_background_store import (
    BRANCH,
    FILES,
    MARKER,
    CarouselCoverBackgroundStore,
    Conflict,
    initial_state,
    validate_state,
)

SHA = "a" * 40
BLOB = "b" * 40


def multiline_base64(raw):
    encoded = base64.b64encode(raw).decode()
    return " \t" + encoded[:5] + "\r\n" + encoded[5:11] + "\f\v" + encoded[11:] + "\n "


def test_load_checks_exact_marker_vercel_and_file_set():
    state = initial_state()
    blobs = {
        "state.json": json.dumps(state).encode(),
        "vercel.json": b'{"git":{"deploymentEnabled":false}}',
        ".carousel-cover-background-data": MARKER.encode(),
    }
    sha_by_name = {name: (str(index + 1) * 40) for index, name in enumerate(sorted(FILES))}
    by_sha = {sha_by_name[name]: value for name, value in blobs.items()}

    def handler(request):
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [
                {"path": name, "type": "blob", "mode": "100644", "sha": sha_by_name[name]}
                for name in FILES
            ]})
        digest = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(by_sha[digest]).decode()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        loaded, sha = CarouselCoverBackgroundStore(client).load()
    assert loaded == state and sha == SHA


def test_save_uses_expected_parent_and_non_force_cas():
    state = initial_state()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/git/trees"):
            body = json.loads(request.content)
            names = {row["path"] for row in body["tree"]}
            assert names == FILES
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.url.path.endswith("/git/commits"):
            assert json.loads(request.content)["parents"] == [SHA]
            return httpx.Response(201, json={"sha": "e" * 40})
        assert request.method == "PATCH"
        assert request.url.path.endswith("/git/refs/heads/" + BRANCH)
        assert json.loads(request.content) == {"sha": "e" * 40, "force": False}
        return httpx.Response(422, json={})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(Conflict),
    ):
        CarouselCoverBackgroundStore(client, "dummy").save(state, SHA)
    assert len(requests) == 3


def test_github_rate_metadata_is_safe_and_available_to_worker():
    reset = "1792700000"

    def handler(request):
        return httpx.Response(404, headers={"x-ratelimit-remaining": "19", "x-ratelimit-reset": reset}, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = CarouselCoverBackgroundStore(client)
        assert store.load()[1] is None
    assert store.remaining == 19 and int(store.reset_at.timestamp()) == int(reset)


def test_state_is_strict_and_rejects_text_credentials_and_invalid_dates():
    state = initial_state()
    assert validate_state(state)
    for field in ("caption", "translation", "token", "authorization"):
        bad = {**state, field: "forbidden"}
        with pytest.raises(ValueError):
            validate_state(bad)
    for field in ("updated_at", "pause_until"):
        with pytest.raises(ValueError):
            validate_state({**state, field: "2026-10-23T12:00:00"})
    with pytest.raises(ValueError):
        validate_state({**state, "pause_reason": "github_pat_" + "a" * 30})


def test_marker_or_extra_code_refuses_existing_branch():
    def handler(request):
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        return httpx.Response(200, json={"tree": [{"path": "app.py", "type": "blob", "mode": "100644", "sha": BLOB}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        CarouselCoverBackgroundStore(client).load()


def test_initial_save_creates_orphan_data_branch_with_no_application_tree():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("git/trees"):
            assert "base_tree" not in body
            assert {row["path"] for row in body["tree"]} == FILES
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.url.path.endswith("git/commits"):
            assert body["parents"] == []
            return httpx.Response(201, json={"sha": "e" * 40})
        assert body == {"ref": "refs/heads/" + BRANCH, "sha": "e" * 40}
        return httpx.Response(201, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert CarouselCoverBackgroundStore(client, "dummy").save(initial_state(), None) == "e" * 40
    assert len(calls) == 3


def test_state_size_limit_and_duplicate_ids_are_rejected(monkeypatch):
    from app.services import carousel_cover_background_store as store
    from app.workers.carousel_cover_background import merge_inventory
    from datetime import datetime, timezone
    state = initial_state()
    merge_inventory(state, [{"id": "1", "periods": ["14d"]}], "existing", datetime.now(timezone.utc))
    state["items"].append(dict(state["items"][0]))
    with pytest.raises(ValueError):
        validate_state(state)
    state["items"].pop()
    monkeypatch.setattr(store, "MAX_BYTES", 100)
    with pytest.raises(ValueError, match="размер"):
        validate_state(state)
