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


@pytest.mark.parametrize("digest", ["eaa" + "a" * 61, "a" * 5 + "eaa" + "a" * 56])
def test_meta_prefix_inside_lowercase_hash_is_safe_metadata(digest):
    state = initial_state()
    state["refs"] = {"live": {"commit": "eaa" + "a" * 37, "blob": "a" * 37 + "eaa"}}
    state["items"] = [{
        "id": "123", "source_hash": digest, "periods": ["7d"],
        "status": "pending", "updated_at": state["updated_at"], "code": None,
        "inflight_started_at": None,
    }]
    assert json.loads(validate_state(state)) == state


@pytest.mark.parametrize("token", ["github_pat_" + "a" * 30, "ghp_" + "a" * 40])
def test_github_tokens_still_rejected_in_otherwise_valid_code(token):
    state = initial_state()
    state["pause_reason"] = token
    with pytest.raises(ValueError, match="В очереди обнаружены запрещённые данные"):
        validate_state(state)


def test_secret_scanner_preserves_exact_meta_prefix_and_case_insensitive_field_names():
    from app.services.carousel_caption_bulk_store import _SECRET

    assert _SECRET.search('"EAA' + 'a' * 45 + '"')
    assert _SECRET.search('"Authorization":')
    # Регистронезависимая эвристика сохраняется; безопасные SHA исключает только validate_state.
    assert _SECRET.search('"' + 'a' * 5 + 'eaa' + 'a' * 56 + '"')


@pytest.mark.parametrize("field", ["caption", "CAPTION", "Translation", "TOKEN", "Authorization", "API_KEY", "service_role_key", "decrypted_secret"])
def test_forbidden_fields_remain_rejected_after_metadata_exclusion(field):
    state = initial_state()
    state[field] = "forbidden"
    with pytest.raises(ValueError):
        validate_state(state)


@pytest.mark.parametrize("value", ["EAA" + "a" * 45, "eaa" + "a" * 45])
def test_meta_tokens_cannot_hide_in_untrusted_non_hash_fields(value):
    state = initial_state()
    state["pause_reason"] = value
    with pytest.raises(ValueError):
        validate_state(state)


def test_unvalidated_hash_field_is_not_exempted_from_schema_validation():
    state = initial_state()
    state["refs"] = {"live": {"commit": "EAA" + "a" * 45, "blob": "a" * 40}}
    with pytest.raises(ValueError, match="Некорректный SHA источника"):
        validate_state(state)


def test_classification_version_is_optional_for_existing_checkpoints():
    state = initial_state()
    assert state['classification_version'] == 2
    state.pop('classification_version')
    assert 'classification_version' not in json.loads(validate_state(state))
    state['classification_version'] = 2
    assert json.loads(validate_state(state))['classification_version'] == 2


@pytest.mark.parametrize('value', [None, True, False, 0, -1, '2', 2.0])
def test_classification_version_requires_positive_strict_integer(value):
    state = initial_state()
    state['classification_version'] = value
    with pytest.raises(ValueError, match='Некорректная версия классификации'):
        validate_state(state)
