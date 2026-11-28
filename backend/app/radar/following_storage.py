"""Серверное чтение проверенных снимков из закрытого bucket Supabase."""
from __future__ import annotations

import os
import re
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.radar.following_discovery import DiscoveryError, Snapshot, normalize_input

DEFAULT_BUCKET = "reelscribe-discovery"
BUCKET_ENV = "FOLLOWING_DISCOVERY_STORAGE_BUCKET"
_MISSING_CODES = {"nosuchkey", "objectnotfound", "not_found", "not found"}
_INVALID = "Снимок поиска авторов не соответствует формату."
_UNAVAILABLE = "Облачное хранилище поиска авторов временно недоступно."


def decode_snapshot(content: bytes, username: str) -> Snapshot:
    try:
        snapshot = Snapshot.model_validate_json(content)
    except ValidationError:
        raise DiscoveryError(503, _INVALID) from None
    has_result = snapshot.status in ("complete", "partial") or (snapshot.status == "running" and bool(snapshot.rows))
    if snapshot.username != username or not has_result:
        raise DiscoveryError(503, _INVALID)
    return snapshot


def storage_project_url() -> httpx.URL:
    configured = os.environ.get("SUPABASE_URL")
    if not configured:
        from app.core.config import settings

        configured = settings.supabase_url
    message = "Некорректный адрес проекта Supabase для снимков поиска авторов."
    try:
        url = httpx.URL(configured)
    except httpx.InvalidURL:
        raise DiscoveryError(503, message) from None
    if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment or url.path not in ("", "/"):
        raise DiscoveryError(503, message)
    return url


class StorageReader:
    def __init__(self, bucket: str, project_url: httpx.URL, service_key: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", bucket):
            raise DiscoveryError(503, "Некорректное имя bucket поиска авторов.")
        if not service_key:
            raise DiscoveryError(503, "На сервере не настроен доступ к закрытому хранилищу снимков.")
        self.bucket = bucket
        headers = {"apikey": service_key}
        if not service_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {service_key}"
        self.client = httpx.Client(
            base_url=f"{str(project_url).rstrip('/')}/storage/v1/object/",
            headers=headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
            follow_redirects=False,
        )

    def read(self, username: str) -> Snapshot | None:
        if normalize_input(username) != username:
            raise DiscoveryError(400, "Имя донора облачного снимка должно быть нормализовано.")
        try:
            response = self.client.get(
                f"{self.bucket}/snapshots/{username}.json",
                params={"cacheNonce": uuid4().hex},
                headers={"Cache-Control": "no-cache, no-store"},
            )
        except httpx.RequestError:
            raise DiscoveryError(503, _UNAVAILABLE) from None
        if response.is_success:
            return decode_snapshot(response.content, username)
        if response.status_code in (400, 404):
            try:
                body = response.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                code = str(body.get("code") or body.get("error") or "").lower()
                message = str(body.get("message", "")).lower()
                if code in _MISSING_CODES and "bucket" not in message:
                    return None
        raise DiscoveryError(503, _UNAVAILABLE)


def read_snapshot(username: str) -> Snapshot | None:
    bucket = os.environ.get(BUCKET_ENV, "").strip()
    if not bucket and os.environ.get("VERCEL") == "1":
        bucket = DEFAULT_BUCKET
    if not bucket:
        return None
    from app.core.config import settings

    storage = StorageReader(bucket, storage_project_url(), settings.supabase_service_role_key.strip())
    with storage.client:
        return storage.read(username)
