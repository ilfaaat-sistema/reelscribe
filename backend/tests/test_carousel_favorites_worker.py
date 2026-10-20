import httpx
import pytest
from app.workers.carousel_favorites import historical_collectors_active


def test_historical_gate_skips_meta_when_any_collector_is_active():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        active = 'carousels-fortnight.yml' in str(request.url) and request.url.params['status'] == 'in_progress'
        return httpx.Response(200, json={'total_count': int(active)})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert historical_collectors_active(client, 'dummy') is True
    assert len(calls) == 3


def test_historical_gate_fails_closed_on_unavailable_api():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client, pytest.raises(httpx.HTTPStatusError):
        historical_collectors_active(client, 'dummy')
