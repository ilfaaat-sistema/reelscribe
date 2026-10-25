"""Настоящий SDK с локальным транспортом: Vertex, ADC и один HTTP-запрос."""
from types import SimpleNamespace
import base64
import json

import httpx
import pytest
from app.services import carousel_cover_vertex as vertex
from google import genai
from google.oauth2.credentials import Credentials
from pydantic import ValidationError


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429, 503, 504])
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
    assert captured['http_options'].timeout == 120_000
    assert len(requests) == 1, repr(caught.value)
    assert requests[0].url.host == 'aiplatform.googleapis.com'
    assert '/projects/test-cover-project/locations/global/' in requests[0].url.path
    assert 'x-goog-api-key' not in requests[0].headers
    assert requests[0].headers['x-server-timeout'] == '120'
    payload = json.loads(requests[0].content)
    assert payload['systemInstruction']['parts'] == [{'text': vertex.PROMPT}]
    assert len(payload['contents']) == 1
    content = payload['contents'][0]
    assert content['role'] == 'user' and len(content['parts']) == 1
    assert set(content['parts'][0]) == {'inlineData'}
    image = content['parts'][0]['inlineData']
    assert base64.urlsafe_b64decode(image['data']) == b'\xff\xd8\xfftest'
    assert image.get('mimeType', image.get('mime_type')) == 'image/jpeg'
    assert payload['generationConfig']['responseMimeType'] == 'application/json'


@pytest.mark.asyncio
@pytest.mark.parametrize('output,expected_status', [
    ({'status': 'no_text', 'source_text': None, 'text_ru': None}, 'no_text'),
    ({'status': 'no_text'}, 'no_text'),
    ({'status': 'unreadable', 'source_text': None}, 'unreadable'),
    ({'status': 'ready', 'source_text': 'Title', 'text_ru': 'Заголовок'}, 'ready'),
    ({'status': 'ready', 'source_text': None, 'text_ru': 'Домысел'}, None),
    ({'status': 'not_needed', 'text_ru': ''}, None),
    ('не JSON: закрытый ответ провайдера', None),
])
async def test_returned_sdk_response_preserves_usage_before_app_validation(monkeypatch, output, expected_status):
    from google.genai import types
    monkeypatch.setattr(vertex, 'get_settings', lambda: SimpleNamespace(vertex_model='gemini-test'))
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            'candidates': [{'content': {'role': 'model', 'parts': [{
                'text': output if isinstance(output, str) else json.dumps(output)}]}, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 3, 'totalTokenCount': 17,
                              'thoughtsTokenCount': 2, 'cachedContentTokenCount': 0}})
    client = genai.Client(vertexai=True, project='test-cover-project', location='global',
                          credentials=Credentials(token='fake-local-adc-token'),
                          http_options=types.HttpOptions(timeout=120_000,
                              retry_options=types.HttpRetryOptions(attempts=1),
                              httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    expected_usage = {'prompt_token_count': 12, 'candidates_token_count': 3, 'total_token_count': 17,
                      'thoughts_token_count': 2, 'cached_content_token_count': 0}
    try:
        if expected_status is None:
            with pytest.raises(vertex.CompletedCoverResponseError) as caught:
                await vertex.generate(client, b'local-test-image', 'image/jpeg')
            assert caught.value.usage == {**expected_usage, 'error_type': 'ValidationError',
                                          'provider_response_complete': True}
            assert 'закрытый ответ' not in str(caught.value)
        else:
            result, usage = await vertex.generate(client, b'local-test-image', 'image/jpeg')
            assert result['status'] == expected_status
            assert usage == expected_usage
            if expected_status in ('no_text', 'unreadable'):
                assert result['source_text'] == result['text_ru'] == ''
    finally:
        await client.aio.aclose()
        client.close()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_sdk_validation_exception_does_not_prove_returned_response(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(vertex, 'get_settings', lambda: SimpleNamespace(vertex_model='gemini-test'))
    error = ValidationError.from_exception_data('SDK', [])
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=AsyncMock(side_effect=error))))
    with pytest.raises(ValidationError) as caught:
        await vertex.generate(client, b'local-test-image', 'image/jpeg')
    assert caught.value is error
    assert isinstance(client.aio.models.generate_content.call_args.kwargs['config'].response_schema, dict)
