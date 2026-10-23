"""Проверки GitHub CAS-хранилища без сети и учётных данных."""
import base64
import json

import httpx
import pytest
from app.services.carousel_caption_bulk_store import (
    BRANCH,
    FILES,
    MARKER,
    CarouselCaptionBulkStore,
    Conflict,
    initial_state,
    validate_state,
)

SHA = "a" * 40
BLOB = "b" * 40


def multiline_base64(raw):
    encoded = base64.b64encode(raw).decode()
    return " \t" + encoded[:5] + "\r\n" + encoded[5:11] + "\f\v" + encoded[11:] + "\n "


def test_state_has_no_caption_translation_or_credentials_and_is_bounded():
    state = initial_state()
    state["items"] = [{
        "id": "123", "source_hash": "c" * 64, "periods": ["30d", "14d"],
        "status": "pending", "updated_at": state["updated_at"], "code": None,
        "inflight_started_at": None,
    }]
    text = validate_state(state)
    assert "caption" not in text and "translation\"" not in text and "token" not in text
    for forbidden in ("caption", "translation", "token", "authorization", "api_key"):
        bad = json.loads(text)
        bad[forbidden] = "secret"
        with pytest.raises(ValueError):
            validate_state(bad)


def test_source_reads_contents_or_blob_without_download_url_or_redirect():
    payload = {"items": []}
    calls = []

    def handler(request):
        calls.append(request)
        if "/contents/" in request.url.path:
            return httpx.Response(200, json={
                "type": "file", "sha": BLOB, "encoding": "none",
                "download_url": "https://attacker.invalid/secret",
            })
        return httpx.Response(200, json={"encoding": "base64", "content": multiline_base64(json.dumps(payload).encode())})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        value = CarouselCaptionBulkStore(client, "dummy").read_source_json("data/branch", "state.json", BLOB)
    assert value == payload
    assert all(request.url.host == "api.github.com" for request in calls)
    assert calls[0].headers["authorization"] == "Bearer dummy"
    assert "dummy" not in str(calls[0].url)


def test_source_contents_accepts_multiline_base64_with_only_ascii_whitespace():
    payload = {"version": 1, "items": []}

    def handler(request):
        return httpx.Response(200, json={
            "type": "file", "sha": BLOB, "encoding": "base64",
            "content": multiline_base64(json.dumps(payload).encode()),
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        value = CarouselCaptionBulkStore(client).read_source_json("data/branch", "state.json", BLOB)
    assert value == payload


@pytest.mark.parametrize("bad", ["!", "\u00a0"])
def test_source_contents_still_rejects_non_base64_and_non_ascii_whitespace(bad):
    encoded = base64.b64encode(b'{"items":[]}').decode()

    def handler(request):
        return httpx.Response(200, json={
            "type": "file", "sha": BLOB, "encoding": "base64", "content": encoded[:4] + bad + encoded[4:],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError):
        CarouselCaptionBulkStore(client).read_source_json("data/branch", "state.json", BLOB)


def test_load_checks_exact_marker_vercel_and_file_set():
    state = initial_state()
    blobs = {
        "state.json": json.dumps(state).encode(),
        "vercel.json": b'{"git":{"deploymentEnabled":false}}',
        ".carousel-caption-bulk-data": MARKER.encode(),
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
        loaded, sha = CarouselCaptionBulkStore(client).load()
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
        CarouselCaptionBulkStore(client, "dummy").save(state, SHA)
    assert len(requests) == 3


def test_github_rate_metadata_is_safe_and_available_to_worker():
    reset = "1792700000"

    def handler(request):
        return httpx.Response(404, headers={"x-ratelimit-remaining": "19", "x-ratelimit-reset": reset}, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = CarouselCaptionBulkStore(client)
        assert store.load()[1] is None
    assert store.remaining == 19 and int(store.reset_at.timestamp()) == int(reset)
