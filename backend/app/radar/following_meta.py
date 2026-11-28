"""Обогащение известных профилей через Meta Graph Business Discovery."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from typing import Literal, NamedTuple, Self

import httpx

from app.radar.activity_job import LIMIT_CODES, USAGE_FIELDS, _retry_after, _usage

_GRAPH_HOST = "graph.facebook.com"
_REQUEST_TIMEOUT = 4.0
_RATE_PAUSE = 900
_FIELDS = "username,name,biography,followers_count,follows_count,media_count"
_LIMIT_CODES = LIMIT_CODES | {80000, 80001, 80003, 80004, 80005, 80006}


class MetaProfile(NamedTuple):
    values: dict | None
    cooldown: int = 0


class MetaError(Exception):
    def __init__(self, kind: Literal["configuration", "auth", "rate", "temporary"], cooldown: int = 0):
        self.kind = kind
        self.cooldown = cooldown



def _caused_by(error: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, error_type):
            return True
        current = current.__cause__ or current.__context__
    return False


def _resolve_graph_ip(exclude: str | None = None) -> str | None:
    try:
        answer = subprocess.run(
            ("/usr/bin/dig", "+short", "+time=3", "+tries=1", _GRAPH_HOST, "A"),
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    for line in answer.stdout.splitlines():
        try:
            address = ipaddress.IPv4Address(line.strip())
        except ipaddress.AddressValueError:
            continue
        if address.is_global and not address.is_multicast and not address.is_reserved and str(address) != exclude:
            return str(address)
    return None


def _business_usage(headers: httpx.Headers) -> tuple[int, int]:
    try:
        data = json.loads(headers.get("x-business-use-case-usage", "{}"))
    except ValueError:
        return 0, 0
    if not isinstance(data, dict):
        return 0, 0
    usage, wait = 0, 0
    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "instagram":
                continue
            for field in USAGE_FIELDS:
                value = entry.get(field)
                if type(value) is int:
                    usage = max(usage, value)
            minutes = entry.get("estimated_time_to_regain_access")
            if type(minutes) is int:
                wait = max(wait, minutes * 60)
    return usage, wait


class Meta:
    def __init__(self):
        token = os.environ.get("META_IG_TOKEN", "")
        account = os.environ.get("META_IG_USER_ID", "")
        version = os.environ.get("META_IG_API_VERSION")
        if not (token and account and version):
            from app.core.config import settings

            token = token or settings.meta_ig_token
            account = account or settings.meta_ig_user_id
            version = version or settings.meta_ig_api_version
        if not token or not re.fullmatch(r"[0-9]{1,40}", account) or not re.fullmatch(r"v[0-9]{1,2}\.[0-9]{1,2}", version):
            raise MetaError("configuration")
        self.url = httpx.URL(f"https://{_GRAPH_HOST}/{version}/{account}")
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, trust_env=False, verify=True, follow_redirects=False,
        )
        self._fallback_ip: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def _get_by_ip(self, address: str, fields: str) -> httpx.Response:
        return self.client.get(
            self.url.copy_with(host=address), params={"fields": fields}, timeout=_REQUEST_TIMEOUT,
            headers={"Host": _GRAPH_HOST}, extensions={"sni_hostname": _GRAPH_HOST},
        )

    def _get(self, fields: str) -> httpx.Response:
        address = self._fallback_ip
        if address is None:
            try:
                return self.client.get(self.url, params={"fields": fields}, timeout=_REQUEST_TIMEOUT)
            except httpx.ConnectError as exc:
                if sys.platform != "darwin" or not _caused_by(exc, socket.gaierror):
                    raise
                address = _resolve_graph_ip()
                if address is None:
                    raise
        try:
            response = self._get_by_ip(address, fields)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self._fallback_ip = None
            if _caused_by(exc, ssl.SSLError):
                raise
            replacement = _resolve_graph_ip(exclude=address)
            if replacement is None:
                raise
            address = replacement
            response = self._get_by_ip(address, fields)
        self._fallback_ip = address
        return response

    def lookup(self, username: str) -> MetaProfile:
        fields = f"business_discovery.username({username}){{{_FIELDS}}}"
        try:
            response = self._get(fields)
        except httpx.RequestError:
            raise MetaError("temporary") from None
        retry_after = _retry_after(response.headers, datetime.now(timezone.utc))
        business_usage, business_wait = _business_usage(response.headers)
        rate_wait = max(_RATE_PAUSE, retry_after, business_wait)
        if response.status_code == 429:
            raise MetaError("rate", rate_wait)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        code = error.get("code")
        if type(code) is int and code in _LIMIT_CODES:
            raise MetaError("rate", rate_wait)
        if response.status_code >= 500 or error.get("is_transient"):
            raise MetaError("temporary", retry_after)
        message = str(error.get("message", "")).lower()
        if (response.status_code in (401, 403) or code in (10, 190, 200, 294)
                or "nonexisting field" in message or "unknown field" in message):
            raise MetaError("auth")
        if not isinstance(payload, dict):
            raise MetaError("temporary", retry_after)
        usage = max(business_usage, *_usage(response.headers).values(), 0)
        cooldown = rate_wait if usage >= 90 or business_wait or retry_after else 0
        profile = payload.get("business_discovery")
        graph_success = response.is_success and not payload.get("error")
        if graph_success and isinstance(profile, dict):
            if not isinstance(profile.get("username"), str) or profile["username"].lower() != username:
                raise MetaError("temporary", retry_after)
            return MetaProfile(profile, cooldown)
        no_profile = error.get("error_subcode") == 2207013 or message == "invalid user id"
        empty_discovery = profile is None and ("business_discovery" in payload or payload.get("id") == self.url.path.rsplit("/", 1)[-1])
        if no_profile or (graph_success and empty_discovery):
            return MetaProfile(None, cooldown)
        raise MetaError("temporary", retry_after)
