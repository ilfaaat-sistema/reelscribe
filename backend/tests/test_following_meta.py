from __future__ import annotations

import json
import socket
import ssl
import subprocess

import httpx
import pytest

from app.radar import following_meta as meta


@pytest.fixture(autouse=True)
def graph_configuration(monkeypatch):
    monkeypatch.setenv("META_IG_TOKEN", "test-meta")
    monkeypatch.setenv("META_IG_USER_ID", "123")
    monkeypatch.setenv("META_IG_API_VERSION", "v23.0")


def _mock_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(meta.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def _dns_error(request):
    raise httpx.ConnectError("ошибка разрешения имени", request=request) from socket.gaierror(socket.EAI_AGAIN)


def _profile():
    return {"business_discovery": {"username": "author", "followers_count": 0}}


def test_dig_uses_system_dns_and_only_public_ipv4(monkeypatch):
    command = ("/usr/bin/dig", "+short", "+time=3", "+tries=1", "graph.facebook.com", "A")
    output = "graph.example.\n::1\n127.0.0.1\n10.0.0.1\n169.254.169.254\n100.64.0.1\n224.0.0.2\n157.240.22.3\n157.240.22.4\n"

    def dig(args, **kwargs):
        assert args == command
        assert kwargs["env"] == {"LC_ALL": "C"}
        assert kwargs["timeout"] == 4
        assert kwargs["check"]
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(meta.subprocess, "run", dig)
    assert meta._resolve_graph_ip() == "157.240.22.3"
    assert meta._resolve_graph_ip(exclude="157.240.22.3") == "157.240.22.4"


def test_dig_timeout_returns_without_an_address(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(meta.subprocess, "run", timeout)
    assert meta._resolve_graph_ip() is None


def test_dns_fallback_preserves_host_and_refreshes_once_on_lost_address(monkeypatch):
    monkeypatch.setattr(meta.sys, "platform", "darwin")
    addresses, requests = [], []

    def resolve(exclude=None):
        addresses.append(exclude)
        return "157.240.22.3" if exclude is None else "157.240.22.4"

    def handler(request):
        requests.append(request.url.host)
        assert "access_token" not in request.url.params
        if request.url.host == "graph.facebook.com":
            _dns_error(request)
        assert request.url.path == "/v23.0/123"
        assert request.headers["Host"] == "graph.facebook.com"
        assert request.extensions["sni_hostname"] == "graph.facebook.com"
        assert request.headers["Authorization"] == "Bearer test-meta"
        if requests == ["graph.facebook.com", "157.240.22.3", "157.240.22.3"]:
            raise httpx.ConnectTimeout("адрес перестал отвечать", request=request)
        return httpx.Response(200, json=_profile())

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr(meta, "_resolve_graph_ip", resolve)
    with meta.Meta() as client:
        assert client.lookup("author").values == _profile()["business_discovery"]
        assert client.lookup("author").values == _profile()["business_discovery"]
        assert client.lookup("author").values == _profile()["business_discovery"]
    assert addresses == [None, "157.240.22.3"]
    assert requests == ["graph.facebook.com", "157.240.22.3", "157.240.22.3", "157.240.22.4", "157.240.22.4"]


@pytest.mark.parametrize(("platform", "dns"), [("linux", True), ("darwin", False)])
def test_regular_network_failure_or_other_os_skips_fallback(monkeypatch, platform, dns):
    def handler(request):
        if dns:
            _dns_error(request)
        raise httpx.ConnectError("соединение отклонено", request=request)

    monkeypatch.setattr(meta.sys, "platform", platform)
    monkeypatch.setattr(meta, "_resolve_graph_ip", lambda **_: pytest.fail("Резервный DNS включился без основания"))
    _mock_transport(monkeypatch, handler)
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert error.value.kind == "temporary"


def test_ip_connection_retries_are_bounded(monkeypatch):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            _dns_error(request)
        tried.append(request.url.host)
        raise httpx.ConnectTimeout("адрес недоступен", request=request)

    tried = []
    monkeypatch.setattr(meta.sys, "platform", "darwin")
    monkeypatch.setattr(meta, "_resolve_graph_ip", lambda exclude=None: "157.240.22.3" if not exclude else "157.240.22.4")
    _mock_transport(monkeypatch, handler)
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert error.value.kind == "temporary"
    assert tried == ["157.240.22.3", "157.240.22.4"]


def test_httpcore_checks_certificate_for_original_hostname(monkeypatch):
    tls = []

    class ConnectedSocket:
        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def close(self):
            pass

    def connect_tcp(address, timeout, source_address=None):
        if address == ("graph.facebook.com", 443):
            raise socket.gaierror(socket.EAI_AGAIN)
        assert address == ("157.240.22.3", 443)
        return ConnectedSocket()

    def wrap_socket(context, sock, *, server_hostname):
        tls.append((server_hostname, context.check_hostname, context.verify_mode))
        raise ssl.SSLCertVerificationError("проверка сертификата не прошла")

    def resolve(exclude=None):
        assert exclude is None
        return "157.240.22.3"

    monkeypatch.setattr(meta.sys, "platform", "darwin")
    monkeypatch.setattr(meta, "_resolve_graph_ip", resolve)
    monkeypatch.setattr(socket, "create_connection", connect_tcp)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert error.value.kind == "temporary"
    assert tls == [("graph.facebook.com", True, ssl.CERT_REQUIRED)]


def test_redirect_does_not_send_token_to_another_host(monkeypatch):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            _dns_error(request)
        assert request.url.host == "157.240.22.3"
        return httpx.Response(302, headers={"Location": "https://outside.example/"})

    monkeypatch.setattr(meta.sys, "platform", "darwin")
    monkeypatch.setattr(meta, "_resolve_graph_ip", lambda: "157.240.22.3")
    _mock_transport(monkeypatch, handler)
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert error.value.kind == "temporary"


def _usage_header(*entries):
    return {"X-Business-Use-Case-Usage": json.dumps({"12345": list(entries)})}


def test_instagram_rate_limit_code_on_http_403_uses_wait_in_minutes(monkeypatch):
    headers = _usage_header({"type": "instagram", "call_count": 100, "estimated_time_to_regain_access": 21})
    _mock_transport(monkeypatch, lambda _: httpx.Response(403, headers=headers, json={"error": {"code": 80002}}))
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert (error.value.kind, error.value.cooldown) == ("rate", 1260)


@pytest.mark.parametrize(("usage", "expected_wait"), [(89, 0), (92, 900)])
def test_buc_uses_instagram_metrics_and_keeps_the_successful_card(monkeypatch, usage, expected_wait):
    headers = _usage_header({"type": "instagram", "total_cputime": usage, "estimated_time_to_regain_access": 0},
                            {"type": "ads_management", "call_count": 100, "estimated_time_to_regain_access": 30})
    _mock_transport(monkeypatch, lambda _: httpx.Response(200, headers=headers, json=_profile()))
    with meta.Meta() as client:
        result = client.lookup("author")
    assert result.values == _profile()["business_discovery"]
    assert result.cooldown == expected_wait


@pytest.mark.parametrize(("status", "payload", "headers", "expected_wait"), [
    (503, {"error": {"message": "Invalid user id"}}, {"Retry-After": "180"}, 180),
    (400, {"error": {"error_subcode": 2207013, "is_transient": True}}, {}, 0),
    (200, {"unexpected": "body"}, {}, 0),
])
def test_temporary_responses_do_not_mark_a_profile_unavailable(monkeypatch, status, payload, headers, expected_wait):
    _mock_transport(monkeypatch, lambda _: httpx.Response(status, headers=headers, json=payload))
    with meta.Meta() as client, pytest.raises(meta.MetaError) as error:
        client.lookup("author")
    assert (error.value.kind, error.value.cooldown) == ("temporary", expected_wait)
