"""Тесты источников разведки (app/radar/sources.py, api/radar_intel.py) — ТЗ 10.

repo_intel подменяется in-memory хранилищем; эндпоинты — через TestClient на отдельном
FastAPI() с роутером разведки (app.main не импортируется)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import radar_intel
from app.radar import intel_settings as cfg
from app.radar import repo_intel, sources


class Store:
    """Мини-хранилище radar_competitors с теми же семантиками, что у repo_intel."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(repo_intel, "get_competitors",
                            lambda names: {n: dict(self.rows[n]) for n in names if n in self.rows})
        monkeypatch.setattr(repo_intel, "count_active_sources",
                            lambda: sum(1 for r in self.rows.values() if r["is_source"]))
        monkeypatch.setattr(repo_intel, "upsert_source", self._upsert)
        monkeypatch.setattr(repo_intel, "mark_excluded", self._exclude)
        monkeypatch.setattr(repo_intel, "mark_restored", self._restore)

    def _upsert(self, name: str) -> None:
        row = self.rows.setdefault(name, {"username": name, "followers": None})
        row.update({"is_source": True, "excluded_at": None, "source_added_at": "now"})

    def _exclude(self, name: str) -> bool:
        row = self.rows.get(name)
        if not row or not row["is_source"]:
            return False
        row.update({"is_source": False, "excluded_at": "now"})
        return True

    def _restore(self, name: str) -> bool:
        row = self.rows.get(name)
        if not row or row["is_source"] or not row.get("excluded_at"):
            return False
        row.update({"is_source": True, "excluded_at": None})
        return True

    def add(self, name: str, **kw: Any) -> None:
        self.rows[name] = {"username": name, "followers": None, "is_source": True,
                           "excluded_at": None, **kw}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store()
    s.install(monkeypatch)
    return s


# ── разбор ввода ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("@Nick", ["nick"]),
    ("nick", ["nick"]),
    ("NICK.name_1", ["nick.name_1"]),
    ("https://www.instagram.com/Nick/", ["nick"]),
    ("instagram.com/nick", ["nick"]),
    ("http://instagram.com/nick/?hl=en", ["nick"]),
    ("https://instagram.com/@nick", ["nick"]),
    ("a, b", ["a", "b"]),
    ("a b\nc,d", ["a", "b", "c", "d"]),
    ("@a, https://instagram.com/b/ c", ["a", "b", "c"]),
    ("a A @a", ["a"]),
    ("", []),
    ("  \n ", []),
])
def test_parse_valid(text, expected):
    names, invalid = sources.parse_input(text)
    assert names == expected and invalid == []


@pytest.mark.parametrize("token", [
    "https://instagram.com/reel/C1abc/",
    "https://www.instagram.com/p/C1abc/",
    "https://instagram.com/tv/C1abc/",
    "https://instagram.com/stories/nick/123/",
    "https://instagram.com/explore/tags/x/",
    "https://instagram.com/",
    "bad-nick",
    "ник",
    "a" * 31,
])
def test_parse_invalid(token):
    names, invalid = sources.parse_input(token)
    assert names == [] and len(invalid) == 1
    assert invalid[0]["input"] == token and invalid[0]["reason"]


def test_parse_mixed_keeps_valid_and_invalid():
    names, invalid = sources.parse_input("ok, https://instagram.com/reel/X/, bad!")
    assert names == ["ok"]
    assert [i["input"] for i in invalid] == ["https://instagram.com/reel/X/", "bad!"]


def test_nick_length_boundary():
    assert sources.parse_input("a" * 30)[0] == ["a" * 30]


# ── добавление ──────────────────────────────────────────────────────────────

def test_add_new_and_already(store):
    store.add("old")
    res = sources.add_sources("@new, old, bad!")
    assert res["added"] == ["new"] and res["already"] == ["old"] and res["restored"] == []
    assert res["invalid"] == [{"input": "bad!", "reason": res["invalid"][0]["reason"]}]
    assert store.rows["new"]["is_source"] is True


def test_add_restores_excluded(store):
    store.add("gone")
    store._exclude("gone")
    res = sources.add_sources("gone")
    assert res["restored"] == ["gone"] and res["added"] == []
    assert store.rows["gone"]["is_source"] is True and store.rows["gone"]["excluded_at"] is None


def test_add_does_not_wipe_followers(store):
    store.rows["x"] = {"username": "x", "followers": 777, "is_source": False, "excluded_at": None}
    sources.add_sources("x")  # разово проверенный аккаунт становится источником
    assert store.rows["x"]["followers"] == 777 and store.rows["x"]["is_source"] is True


def test_add_limit_blocks_everything(store):
    for i in range(cfg.MAX_SOURCES - 1):
        store.add(f"s{i}")
    with pytest.raises(sources.SourceLimitError):
        sources.add_sources("n1, n2")
    assert "n1" not in store.rows and "n2" not in store.rows  # ничего не записано
    assert sources.add_sources("n1")["added"] == ["n1"]  # ровно до лимита — можно


def test_add_limit_not_checked_when_only_already(store):
    for i in range(cfg.MAX_SOURCES):
        store.add(f"s{i}")
    assert sources.add_sources("s1")["already"] == ["s1"]


# ── исключить / вернуть ─────────────────────────────────────────────────────

def test_exclude_and_restore(store):
    store.add("a")
    sources.exclude_source("@A")
    assert store.rows["a"]["is_source"] is False and store.rows["a"]["excluded_at"]
    sources.restore_source("a")
    assert store.rows["a"]["is_source"] is True


def test_exclude_not_source_404(store):
    with pytest.raises(sources.SourceNotFoundError):
        sources.exclude_source("nobody")


def test_restore_not_excluded(store):
    store.add("a")
    with pytest.raises(sources.SourceNotFoundError):
        sources.restore_source("a")  # активный, не исключённый
    store.rows["once"] = {"username": "once", "is_source": False, "excluded_at": None}
    with pytest.raises(sources.SourceNotFoundError):
        sources.restore_source("once")  # разово проверенный
    with pytest.raises(sources.SourceNotFoundError):
        sources.restore_source("nobody")


def test_restore_respects_limit(store):
    for i in range(cfg.MAX_SOURCES):
        store.add(f"s{i}")
    store.rows["z"] = {"username": "z", "is_source": False, "excluded_at": "now"}
    with pytest.raises(sources.SourceLimitError):
        sources.restore_source("z")


# ── эндпоинты ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(radar_intel.router, prefix="/api")
    return TestClient(app)


def test_api_post_sources(client, store):
    resp = client.post("/api/radar/sources", json={"input": "@nick, https://instagram.com/other/, https://instagram.com/p/X/"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == ["nick", "other"] and body["restored"] == [] and body["already"] == []
    assert body["invalid"][0]["input"] == "https://instagram.com/p/X/"


def test_api_post_sources_empty_400(client):
    assert client.post("/api/radar/sources", json={"input": "  "}).status_code == 400
    assert client.post("/api/radar/sources", json={}).status_code == 422


def test_api_post_sources_limit_400(client, store):
    for i in range(cfg.MAX_SOURCES):
        store.add(f"s{i}")
    resp = client.post("/api/radar/sources", json={"input": "extra"})
    assert resp.status_code == 400 and "Лимит" in resp.json()["detail"]


def test_api_delete_and_restore(client, store):
    store.add("a")
    assert client.delete("/api/radar/sources/a").json() == {"removed": True}
    assert client.delete("/api/radar/sources/a").status_code == 404
    assert client.post("/api/radar/sources/a/restore").json() == {"restored": True}
    assert client.post("/api/radar/sources/a/restore").status_code == 404


def test_api_feed_validation_and_delegation(client, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_build_feed(**kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"total": 0, "items": []}

    monkeypatch.setattr(radar_intel.feed, "build_feed", fake_build_feed)
    assert client.get("/api/radar/feed?period=5").status_code == 400
    assert client.get("/api/radar/feed?limit=201").status_code == 422
    resp = client.get("/api/radar/feed?period=7&usernames=A,@b&only_viral=1&sort=bogus&order=asc&limit=10&offset=5")
    assert resp.status_code == 200
    assert calls[-1] == {"period": 7, "usernames": ["a", "b"], "only_viral": True, "sort": "viral",
                         "order": "asc", "limit": 10, "offset": 5}
    client.get("/api/radar/feed")
    assert calls[-1]["period"] == 30 and calls[-1]["order"] == "desc" and calls[-1]["usernames"] == []


def test_api_get_sources_delegates(client, monkeypatch):
    monkeypatch.setattr(radar_intel.feed, "build_sources", lambda: {"active": [], "excluded": [], "max_sources": 60})
    assert client.get("/api/radar/sources").json()["max_sources"] == 60
