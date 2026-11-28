"""HTTP API отдельного снимка подписок донора."""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.radar import following_discovery as discovery

router = APIRouter(prefix="/radar/discovery", tags=["radar-discovery"])
_NO_CACHE = {"Cache-Control": "no-store"}
_DEFAULT_ORIGINS = frozenset({"http://localhost:5177", "http://127.0.0.1:5177", "http://[::1]:5177"})
_LOCAL_ORIGIN = re.compile(r"http://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?")


class DiscoveryInput(BaseModel):
    input: str = Field(max_length=500)


def _loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback
    except ValueError:
        return False


def _local_request(request: Request, *, require_origin: bool) -> bool:
    if os.environ.get("FOLLOWING_DISCOVERY_LOCAL_START") != "1" or os.environ.get("VERCEL") == "1":
        return False
    server = request.scope.get("server")
    if not request.client or not _loopback(request.client.host) or not server or not _loopback(server[0]):
        return False
    headers = request.headers
    if any(key == "forwarded" or key == "x-real-ip" or key.startswith("x-forwarded-") for key in headers):
        return False
    host, origin = headers.get("host"), headers.get("origin")
    fetch_site = headers.get("sec-fetch-site")
    if len(headers.getlist("host")) != 1 or len(headers.getlist("origin")) > 1:
        return False
    if fetch_site not in (None, "same-origin") or (origin is None and (require_origin or fetch_site is None)):
        return False
    local_origin = os.environ.get("FOLLOWING_DISCOVERY_LOCAL_ORIGIN")
    allowed = {local_origin} if local_origin else _DEFAULT_ORIGINS
    if origin is None:
        origin = f"http://{host}"
    return origin in allowed and bool(_LOCAL_ORIGIN.fullmatch(origin)) and urlsplit(origin).netloc == host


@router.get("/{username}", response_model=discovery.Snapshot)
def get_discovery(username: str, request: Request, response: Response) -> discovery.Snapshot:
    response.headers.update(_NO_CACHE)
    try:
        snapshot = discovery.get(discovery.normalize_input(username), advance=_local_request(request, require_origin=False))
    except discovery.DiscoveryError as exc:
        raise HTTPException(exc.status, exc.message, headers=_NO_CACHE) from None
    if snapshot is None:
        raise HTTPException(404, "Сохранённый список подписок для этого профиля не найден.", headers=_NO_CACHE)
    return snapshot


@router.post("", response_model=discovery.Snapshot)
def start_discovery(body: DiscoveryInput, request: Request, response: Response) -> discovery.Snapshot:
    response.headers.update(_NO_CACHE)
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    allow_start = media_type == "application/json" and _local_request(request, require_origin=True)
    try:
        snapshot = discovery.start(discovery.normalize_input(body.input), allow_start=allow_start)
    except discovery.DiscoveryError as exc:
        raise HTTPException(exc.status, exc.message, headers=_NO_CACHE) from None
    if snapshot.status == "running":
        response.status_code = 202
    return snapshot
