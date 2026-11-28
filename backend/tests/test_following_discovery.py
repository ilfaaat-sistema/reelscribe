from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.radar_discovery import router
from app.radar import following_apify as apify
from app.radar import following_discovery as service

PATH = "/api/radar/discovery"
ORIGIN = "http://127.0.0.1:5177"
BROWSER_POST = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
BROWSER_GET = {"Sec-Fetch-Site": "same-origin"}


def app_client(peer="127.0.0.1", base_url=ORIGIN):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app, client=(peer, 45000), base_url=base_url)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    calls = []
    routes = {}
    now = [datetime(2026, 11, 28, tzinfo=timezone.utc)]
    real_client = httpx.Client

    def transport(request):
        key = (request.method, request.url.host, request.url.path)
        calls.append(request)
        assert key in routes, key
        result = routes[key]
        return result(request) if callable(result) else httpx.Response(200, json=result)

    def transport_client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(httpx, "Client", transport_client)
    monkeypatch.setattr(service, "_tokens", lambda **_: {"test-ref": "test-apify"})
    monkeypatch.setattr(service, "_now", lambda: now[0])
    monkeypatch.setenv("FOLLOWING_DISCOVERY_DIR", str(tmp_path))
    monkeypatch.setenv("FOLLOWING_DISCOVERY_LOCAL_START", "1")
    monkeypatch.setenv("FOLLOWING_DISCOVERY_MAX_CHARGE_USD", "5")
    monkeypatch.setenv("META_IG_TOKEN", "test-meta")
    monkeypatch.setenv("META_IG_USER_ID", "123")
    monkeypatch.setenv("META_IG_API_VERSION", "v23.0")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("FOLLOWING_DISCOVERY_LOCAL_ORIGIN", raising=False)
    return tmp_path, routes, calls, now


def _snapshot(rows):
    return {"username": "donor", "status": "complete", "expected": len(rows), "collected": len(rows),
            "checked_at": "2026-11-28T00:00:00Z", "reason": None, "rows": rows}


def _row(username, **overrides):
    return {"username": username, "name": None, "bio": None, "followers": None, "following": None,
            "posts": None, "private": False, "profile_url": f"https://www.instagram.com/{username}/", **overrides}


def test_import_is_independent_and_links_are_canonical(fixture, monkeypatch):
    directory, _, calls, _ = fixture
    monkeypatch.delenv("FOLLOWING_DISCOVERY_LOCAL_START")
    monkeypatch.delenv("META_IG_TOKEN")

    def fail_on_secret_access(**_):
        pytest.fail("Чтение снимка не должно обращаться к Apify")

    monkeypatch.setattr(service, "_tokens", fail_on_secret_access)
    source = _snapshot([_row("Author", name="Автор", followers=0, profile_url="https://wrong.example/",
                              reel_url="https://instagram.com/author/p/Real_1/?anything=1", note="лишнее",
                              profile_source="Ручная проверка", profile_checked_at="2026-11-01")])
    (directory / "donor.json").write_text(json.dumps(source), encoding="utf-8")
    with app_client("192.0.2.20") as client:
        fetched = client.get(f"{PATH}/@DoNoR")
        assert fetched.status_code == 200
        assert fetched.headers["Cache-Control"] == "no-store"
        row = fetched.json()["rows"][0]
        assert (row["username"], row["name"], row["followers"], row["posts"]) == ("author", "Автор", 0, None)
        assert row["profile_url"] == "https://www.instagram.com/author/"
        assert row["reel_url"] == "https://www.instagram.com/p/Real_1/"
        assert row["profile_source"] == "Ручная проверка"
        assert row["profile_checked_at"] == "2026-11-01"
        assert "note" not in row
        cached = client.post(PATH, json={"input": "instagram.com/DoNoR/?igsh=example"})
        assert cached.json() == fetched.json()
        assert client.get(f"{PATH}/absent").status_code == 404
        assert client.post(PATH, json={"input": "absent"}, headers=BROWSER_POST).status_code == 403
    assert not calls


@pytest.mark.parametrize("value", ["../donor", "https://evil.example/donor", "https://instagram.com.evil.example/donor",
                                   "https://instagram.com@evil.example/donor", "https://www.instagram.com/p/code/",
                                   "instagram.com/donor/following", "foo bar"])
def test_reject_invalid_input(fixture, value):
    with app_client() as client:
        assert client.post(PATH, json={"input": value}, headers=BROWSER_POST).status_code == 400
    assert not fixture[2]


@pytest.mark.parametrize(("peer", "base_url", "headers"), [
    ("192.0.2.20", ORIGIN, BROWSER_POST),
    ("127.0.0.1", "http://192.0.2.20:5177", {**BROWSER_POST, "Host": "127.0.0.1:5177"}),
    ("192.0.2.20", ORIGIN, {**BROWSER_POST, "X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", ORIGIN, {**BROWSER_POST, "X-Forwarded-Host": "127.0.0.1:5177"}),
    ("127.0.0.1", ORIGIN, {**BROWSER_POST, "Origin": "https://external.example"}),
    ("127.0.0.1", ORIGIN, {**BROWSER_POST, "Sec-Fetch-Site": "cross-site"}),
    ("127.0.0.1", ORIGIN, {"Sec-Fetch-Site": "same-origin"}),
])
def test_paid_start_requires_local_transport_and_origin(fixture, peer, base_url, headers):
    with app_client(peer, base_url) as client:
        assert client.post(PATH, json={"input": "donor"}, headers=headers).status_code == 403
    assert not fixture[2]


def _account(routes, usage=0.01, delayed=0.0):
    routes[("GET", "api.apify.com", "/v2/users/me/limits")] = {
        "data": {"limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": delayed}}}
    routes[("GET", "api.apify.com", "/v2/users/me/usage/monthly")] = {
        "data": {"totalUsageCreditsUsdAfterVolumeDiscount": usage}}


def _graph(routes, profiles):
    def resolve(request):
        name = request.url.params["fields"].split("(")[1].split(")")[0]
        assert request.headers["Authorization"] == "Bearer test-meta"
        assert "access_token" not in request.url.params
        result = profiles[name]
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    routes[("GET", "graph.facebook.com", "/v23.0/123")] = resolve


def _dataset(routes, *, coverage="FULL_AVAILABLE_SET", expected=3, stop="end"):
    donor = {"username": "donor", "followingCount": expected}
    summary = {"rowType": "summary", "status": "SUCCESS", "sourceProfile": donor,
               "relationshipType": "FOLLOWING", "coverageStatus": coverage,
               "coverage": {"listCount": expected, "rowsReturned": 3, "stopReason": stop}}
    rows = []
    for author, private in [("public_author", False), ("private_author", True), ("personal_author", False)]:
        rows.append({"rowType": "relationship", "status": "SUCCESS", "sourceProfile": donor,
                     "relationshipType": "FOLLOWING", "user": {"username": author, "id": author,
                                                                      "isPrivate": private, "fullName": author}})
    other_direction = {**rows[0], "relationshipType": "FOLLOWER", "user": {"username": "wrong_direction"}}
    other_donor = {**rows[0], "sourceProfile": {"username": "other"}, "user": {"username": "wrong_donor"}}
    routes[("GET", "api.apify.com", "/v2/datasets/Dataset123/items")] = [*rows, rows[0], other_direction, other_donor, summary]
    routes[("GET", "api.apify.com", "/v2/actor-runs/Run123")] = {
        "data": {"status": "SUCCEEDED", "defaultDatasetId": "Dataset123"}}


def test_start_poll_and_persist_provider_contract(fixture):
    directory, routes, calls, _ = fixture
    _account(routes)
    _dataset(routes)
    _graph(routes, {"public_author": {"business_discovery": {"username": "public_author", "name": "Живой автор",
                                                               "biography": "Описание", "followers_count": 0,
                                                               "follows_count": 12, "media_count": 22}},
                    "personal_author": httpx.Response(400, json={"error": {"code": 100, "error_subcode": 2207013}})})
    actor_path = f"/v2/actors/{apify.LIST_ACTOR}/runs"
    routes[("POST", "api.apify.com", actor_path)] = {"data": {"id": "Run123"}}
    with app_client() as client:
        started = client.post(PATH, json={"input": "https://www.instagram.com/DoNoR/"}, headers=BROWSER_POST)
        assert started.status_code == 202
        assert started.json()["status"] == "running"
        assert "Run123" not in started.text
        assert "Run123" in (directory / ".discovery-active.json").read_text()
        assert "test-apify" not in (directory / ".discovery-active.json").read_text()
        request = calls[-1]
        assert request.url.path == actor_path
        assert request.url.params["maxTotalChargeUsd"] == "1.88"
        assert request.headers["Authorization"] == "Bearer test-apify"
        assert json.loads(request.content) == {"profiles": ["donor"], "relationshipType": "FOLLOWING",
                                                 "maxFollowingPerProfile": 1500, "includePrivateAccounts": True,
                                                 "includeProfileMetrics": False}
        assert client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST).status_code == 202
        assert client.post(PATH, json={"input": "another"}, headers=BROWSER_POST).status_code == 429
        assert client.get(f"{PATH}/donor").json()["collected"] == 0
        collected = client.get(f"{PATH}/donor", headers=BROWSER_GET).json()
        assert collected["status"] == "running"
        assert collected["collected"] == collected["expected"] == 3
        assert all(req.url.host == "api.apify.com" for req in calls)
        complete = client.get(f"{PATH}/donor", headers=BROWSER_GET).json()
        assert complete["status"] == "complete"
        assert [row["profile_status"] for row in complete["rows"]] == ["available", "private", "unavailable"]
        assert complete["rows"][0]["followers"] == 0
        assert complete["rows"][1]["posts"] is None
        assert len([req for req in calls if req.method == "POST"]) == 1
        assert len([req for req in calls if req.url.host == "graph.facebook.com"]) == 2
        assert json.loads((directory / "donor.json").read_text()) == complete
        assert not (directory / ".discovery-active.json").exists()
        before = len(calls)
        assert client.get(f"{PATH}/donor", headers=BROWSER_GET).json() == complete
        assert len(calls) == before


def test_quota_and_unknown_start_prevent_repeated_paid_calls(fixture):
    _, routes, calls, _ = fixture
    _account(routes, usage=4.9)
    with app_client() as client:
        assert client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST).status_code == 429
        assert all(req.method == "GET" for req in calls)
        _account(routes)
        actor_path = f"/v2/actors/{apify.LIST_ACTOR}/runs"
        routes[("POST", "api.apify.com", actor_path)] = lambda _: httpx.Response(504)
        assert client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST).status_code == 502
        assert client.get(f"{PATH}/donor", headers=BROWSER_GET).json()["status"] == "failed"
        assert client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST).json()["status"] == "failed"
        assert client.post(PATH, json={"input": "other"}, headers=BROWSER_POST).status_code == 429
        assert sum(req.method == "POST" for req in calls) == 1


def test_resume_only_unchecked_and_respects_rate_limit(fixture):
    directory, routes, calls, clock = fixture
    source = _snapshot([_row("checked", followers=0, following=2, posts=1), _row("unchecked", bio="Сохранённое описание", following=7),
                        _row("private", private=True), _row("unavailable", profile_status="unavailable")])
    (directory / "donor.json").write_text(json.dumps(source), encoding="utf-8")
    _graph(routes, {"unchecked": httpx.Response(429, headers={"Retry-After": "1200"})})
    pending, retry = service.resume_meta("donor", max_batches=2)
    assert retry == clock[0] + timedelta(seconds=1200)
    assert pending.rows[1].profile_status == "pending"
    assert service.resume_meta("donor", max_batches=2)[1] == retry
    assert len(calls) == 1
    clock[0] = retry + timedelta(seconds=1)
    _graph(routes, {"unchecked": {"business_discovery": {"username": "unchecked", "followers_count": 5, "follows_count": None, "biography": None}}})
    result, retry = service.resume_meta("donor", max_batches=2)
    assert retry is None
    assert result.status == "complete"
    assert result.expected == 4
    assert result.rows[0].followers == 0
    assert result.rows[1].followers == 5
    assert result.rows[1].following == 7
    assert result.rows[1].bio == "Сохранённое описание"
    assert result.rows[1].profile_source == "Meta Graph API"
    assert result.rows[1].profile_checked_at == clock[0].isoformat()
    assert result.checked_at.isoformat() == "2026-11-28T00:00:00+00:00"
    assert [row.profile_status for row in result.rows] == ["available", "available", "private", "unavailable"]
    assert json.loads((directory / "donor.json").read_text())["rows"][1]["followers"] == 5
    assert all(req.url.host == "graph.facebook.com" for req in calls)
    assert not (directory / ".donor.meta.json").exists()


def test_cutoff_and_dns_failure_keep_found_rows(fixture):
    _, routes, _, clock = fixture
    _account(routes)
    _dataset(routes, coverage="PARTIAL_PUBLIC", expected=10, stop="spendingLimit")
    routes[("POST", "api.apify.com", f"/v2/actors/{apify.LIST_ACTOR}/runs")] = {"data": {"id": "Run123"}}

    def unavailable(request):
        raise httpx.ConnectError("узел временно не отвечает", request=request)

    routes[("GET", "graph.facebook.com", "/v23.0/123")] = unavailable
    with app_client() as client:
        client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST)
        client.get(f"{PATH}/donor", headers=BROWSER_GET)
        response = client.get(f"{PATH}/donor", headers=BROWSER_GET).json()
        assert response["status"] == "running"
        assert response["rows"][0]["profile_status"] == "pending"
        for _ in range(2):
            clock[0] += timedelta(minutes=3)
            response = client.get(f"{PATH}/donor", headers=BROWSER_GET).json()
        assert (response["status"], response["collected"], response["expected"]) == ("partial", 3, 10)
        assert response["rows"][0]["profile_status"] == "not_checked"
        assert "технической ошибки Meta" in response["reason"]


def test_primary_account_is_the_only_account_considered(fixture, monkeypatch):
    _, routes, calls, _ = fixture
    lookups = []

    def tokens(*, primary_only=False):
        lookups.append(primary_only)
        return {"primary": "primary-apify"} if primary_only else {"spare": "spare-apify"}

    monkeypatch.setattr(service, "_tokens", tokens)
    _account(routes, usage=4.9)
    with app_client() as client:
        assert client.post(PATH, json={"input": "donor"}, headers=BROWSER_POST).status_code == 429
    assert lookups == [True]
    assert {request.headers["Authorization"] for request in calls} == {"Bearer primary-apify"}


def test_flat_apify_fields_and_exact_pagination_limit(fixture, monkeypatch):
    _, routes, _, clock = fixture
    monkeypatch.setattr(apify, "_PAGE_SIZE", 2)
    _dataset(routes)
    key = ("GET", "api.apify.com", "/v2/datasets/Dataset123/items")
    source = routes[key][:3] + routes[key][-1:]
    flattened = []
    for row in source:
        item = {}
        for field, value in row.items():
            if isinstance(value, dict):
                item.update({f"{field}.{key}": nested for key, nested in value.items()})
            else:
                item[field] = value
        flattened.append(item)
    flattened[-1]["scrapedAt"] = "2026-11-20T00:00:00Z"

    def page(request):
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        assert "sourceProfile.username" in request.url.params["fields"]
        return httpx.Response(200, json=flattened[offset:offset + limit])

    routes[key] = page
    with apify.Apify("test-apify") as provider:
        result = provider.items("Dataset123", apify.LIST_FIELDS, max_items=4)
        with pytest.raises(apify.ProviderError):
            provider.items("Dataset123", apify.LIST_FIELDS, max_items=3)
    state = service.ActiveRun(username="donor", token_ref="test-ref", checked_at=clock[0])
    service._list_result(state, result, status="SUCCEEDED")
    assert len(state.rows) == state.expected == 3
    assert state.list_reason is None
    assert state.list_checked_at.isoformat() == "2026-11-20T00:00:00+00:00"
