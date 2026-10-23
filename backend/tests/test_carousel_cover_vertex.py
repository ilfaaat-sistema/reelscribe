"""Настоящий SDK с локальным транспортом: Vertex, ADC и один HTTP-запрос."""
from types import SimpleNamespace

import httpx
import pytest
from app.services import carousel_cover_vertex as vertex
from google import genai
from google.oauth2.credentials import Credentials


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429, 503])
async def test_sdk_vertex_uses_adc_and_does_not_retry(monkeypatch, status):
    config = SimpleNamespace(enabled=True, auth_mode='adc', vertex_project='test-cover-project',
                             vertex_location='global', vertex_model='gemini-test')
    monkeypatch.setattr(vertex, 'get_settings', lambda: config)
    monkeypatch.setenv('GOOGLE_API_KEY', 'unused-fake-key')
    monkeypatch.setenv('GEMINI_API_KEY', 'unused-fake-gemini-key')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'code': status, 'message': 'test unavailable'}})
    original = genai.Client
    captured = {}
    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    def local_client(**kwargs):
        captured.update(kwargs)
        kwargs['credentials'] = Credentials(token='fake-local-adc-token')
        kwargs['http_options'].httpx_async_client = transport
        return original(**kwargs)
    monkeypatch.setattr(genai, 'Client', local_client)
    client = vertex.create_vertex_client()
    try:
        with pytest.raises(Exception) as caught:
            await vertex.generate(client, b'\xff\xd8\xfftest', 'image/jpeg')
    finally:
        await client.aio.aclose()
        client.close()
    assert captured['vertexai'] is True
    assert captured['project'] == 'test-cover-project'
    assert 'api_key' not in captured
    assert captured['http_options'].retry_options.attempts == 1
    assert captured['http_options'].timeout == 60_000
    assert len(requests) == 1, repr(caught.value)
    assert requests[0].url.host == 'aiplatform.googleapis.com'
    assert '/projects/test-cover-project/locations/global/' in requests[0].url.path
    assert 'x-goog-api-key' not in requests[0].headers
