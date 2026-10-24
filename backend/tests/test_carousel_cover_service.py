"""Проверки платного пути только на подменах, без внешних обращений."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.services import carousel_cover_service as service
from app.services.carousel_cover_settings import CoverSettings
from app.services.carousel_cover_vertex import CoverResult
from fastapi import HTTPException


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.attempts = 0
        self.held = set()
        self.mode = 'pilot'
        self.daily_attempts = None
        self.reset_at = '2030-01-02T21:00:00Z'

    def budget(self):
        daily = self.attempts if self.daily_attempts is None else self.daily_attempts
        limited = self.attempts >= 20 if self.mode == 'pilot' else daily >= 500
        return {'status': 'limit_reached' if limited else 'available', 'mode': self.mode,
                'pilot_limit': 20, 'daily_limit': 500, 'reset_at': self.reset_at,
                'retry_after': self.reset_at if limited and self.mode == 'live' else None,
                'attempts': self.attempts, 'daily_attempts': daily,
                'active_slots': len(self.held), 'available_slots': max(0, 2-len(self.held))}


    def read(self, ids):
        return [dict(row, media_id=identity) for identity, row in self.rows.items() if identity in ids]

    def read_source_override(self, media_id):
        return None

    def reserve(self, media_id, model, image_hash):
        if media_id in self.rows:
            return self.rows[media_id]
        budget = self.budget()
        if budget['status'] == 'limit_reached':
            return budget
        if len(self.held) >= 2:
            return {'status': 'busy'}
        self.attempts += 1
        if self.daily_attempts is not None:
            self.daily_attempts += 1
        self.held.add(media_id)
        self.rows[media_id] = {'status': 'running'}
        return {'status': 'running', 'claim_token': media_id}

    def finish(self, media_id, token, result, usage, duration_ms, uncertain):
        assert token == media_id
        self.rows[media_id] = {**result, 'attempt_recorded': True}
        if not uncertain:
            self.held.remove(media_id)
        return self.rows[media_id]

    def unavailable(self, media_id, reason_code='image_unavailable', source_kind=None):
        return self.rows.setdefault(media_id, {'status': 'image_unavailable', 'reason_code': reason_code})


@pytest.fixture
def runtime(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(service, 'CoverStore', lambda: store)
    monkeypatch.setattr(service, 'get_settings', lambda: SimpleNamespace(
        enabled=True, vertex_model='model', image_normalization='jpeg2048'))
    monkeypatch.setattr(service.carousels, 'detail', AsyncMock(return_value={'media_url': 'https://x.fbcdn.net/a'}))
    monkeypatch.setattr(service, 'download_image', AsyncMock(return_value=(b'jpg', 'image/jpeg')))
    monkeypatch.setattr(service, 'normalize_image', Mock(return_value=(b'jpg', 'image/jpeg')))
    client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock())
    monkeypatch.setattr(service, 'create_vertex_client', Mock(return_value=client))
    monkeypatch.setattr(service, 'generate', AsyncMock(return_value=({'status': 'ready', 'source_text': 'Hello', 'text_ru': 'Привет'}, {'total_token_count': 8})))
    return store


@pytest.mark.asyncio
async def test_cache_is_shared_between_periods_and_signed_urls(runtime):
    first = await service.translate('1', '7d')
    second = await service.translate('1', 'archive14d')
    assert first['item'] == second['item']
    assert first['attempt_reserved'] is True and second['attempt_reserved'] is False
    assert runtime.attempts == service.generate.await_count == 1
    assert service.carousels.detail.await_count == 1


@pytest.mark.asyncio
async def test_same_id_concurrency_has_one_paid_call(runtime):
    gate = asyncio.Event()
    async def generate(*args):
        await gate.wait()
        return {'status': 'no_text'}, {}
    service.generate.side_effect = generate
    first = asyncio.create_task(service.translate('1', '7d'))
    for _ in range(100):
        if service.generate.await_count:
            break
        await asyncio.sleep(.001)
    second = await service.translate('1', '14d')
    assert second['item']['status'] == 'running'
    gate.set()
    await first
    assert runtime.attempts == service.generate.await_count == 1


@pytest.mark.asyncio
async def test_busy_does_not_call_model(runtime):
    runtime.held.update(('a', 'b'))
    assert (await service.translate('1', '7d'))['item']['status'] == 'busy'
    service.generate.assert_not_awaited()
    assert runtime.attempts == 0


@pytest.mark.asyncio
async def test_21st_attempt_denied(runtime):
    for identity in range(1, 21):
        await service.translate(str(identity), '7d')
    assert (await service.translate('21', '7d'))['item']['status'] == 'limit_reached'
    assert service.generate.await_count == runtime.attempts == 20


@pytest.mark.asyncio
async def test_timeout_is_terminal_and_keeps_unknown_slot(runtime):
    service.generate.side_effect = TimeoutError('provider signed URL secret')
    assert (await service.translate('1', '7d'))['item']['status'] == 'error'
    assert (await service.translate('1', '30d'))['item']['status'] == 'error'
    assert runtime.attempts == service.generate.await_count == 1
    assert '1' in runtime.held


@pytest.mark.asyncio
async def test_disabled_is_closed_before_image_and_vertex(runtime, monkeypatch):
    monkeypatch.setattr(service, 'get_settings', lambda: SimpleNamespace(enabled=False))
    assert (await service.translate('1', '7d'))['item']['status'] == 'disabled'
    service.download_image.assert_not_awaited()
    service.create_vertex_client.assert_not_called()
    assert not CoverSettings(_env_file=None).enabled


@pytest.mark.asyncio
async def test_missing_id_cannot_supply_url(runtime):
    service.carousels.detail.side_effect = HTTPException(404, 'Нет')
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d')
    assert exc.value.status_code == 422
    service.download_image.assert_not_awaited()
    assert runtime.attempts == 0


@pytest.mark.asyncio
async def test_image_unavailable_is_cached_without_attempt(runtime):
    service.download_image.side_effect = service.ImageUnavailable()
    assert (await service.translate('1', '7d'))['item']['status'] == 'image_unavailable'
    assert (await service.translate('1', '14d'))['item']['status'] == 'image_unavailable'
    assert runtime.attempts == 0
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_failure_is_sanitized_and_fail_closed(runtime):
    runtime.read = Mock(side_effect=RuntimeError('credential signed-url'))
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d')
    assert exc.value.status_code == 503
    assert 'credential' not in exc.value.detail
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_only_reads(runtime):
    result = await service.read(['1', '2'])
    assert [row['status'] for row in result['items']] == ['missing', 'missing']
    service.download_image.assert_not_awaited()
    service.generate.assert_not_awaited()


@pytest.mark.parametrize('status', ['no_text', 'unreadable'])
def test_empty_and_unreadable_do_not_invent_text(status):
    result = CoverResult(status=status, source_text='шум', text_ru='домысел')
    assert result.source_text == result.text_ru == ''


def test_ready_result_is_not_reclassified_by_alphabet():
    assert CoverResult(status='ready', source_text='Мой заголовок!', text_ru='Изменённый').status == 'ready'
    assert CoverResult(status='ready', source_text='Мой AI guide', text_ru='Мой гид по ИИ').text_ru == 'Мой гид по ИИ'
    with pytest.raises(ValueError):
        CoverResult(status='ready', source_text='', text_ru='Домысел')


@pytest.mark.asyncio
async def test_stale_running_is_terminal_in_read_without_releasing_slot(runtime):
    from datetime import datetime, timedelta, timezone
    runtime.rows['1'] = {'status': 'running', 'created_at': (datetime.now(timezone.utc) - timedelta(seconds=service.STALE_RUNNING_SECONDS + 1)).isoformat()}
    runtime.attempts = 1
    runtime.held.add('1')
    assert (await service.read(['1']))['items'][0]['status'] == 'error'
    assert (await service.translate('1', '7d'))['item']['status'] == 'error'
    assert runtime.rows['1']['status'] == 'running'
    assert runtime.attempts == 1 and runtime.held == {'1'}
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_but_active_request_is_not_marked_stale(runtime):
    from datetime import datetime, timedelta, timezone
    runtime.rows['1'] = {'status': 'running', 'created_at': (datetime.now(timezone.utc) - timedelta(seconds=130)).isoformat()}
    assert (await service.read(['1']))['items'][0]['status'] == 'running'


@pytest.mark.asyncio
async def test_provider_failure_records_only_safe_metadata(runtime, caplog):
    class ProviderError(Exception):
        code = 503
    service.generate.side_effect = ProviderError('private credential and provider payload')
    original_finish = runtime.finish
    runtime.finish = Mock(wraps=original_finish)
    result = await service.translate('1', '7d')
    assert result['item']['status'] == 'error'
    assert runtime.attempts == 1 and runtime.held == {'1'}
    assert runtime.finish.call_args.args[3] == {
        'error_type': 'ProviderError', 'provider_status': 503, 'provider_response_complete': False,
        'prompt_revision': service.PROMPT_REVISION, 'image_preprocessing': 'jpeg2048', 'source_kind': 'media_url', 'source_failures': []}
    assert 'private credential' not in caplog.text
    assert service.generate.await_count == 1


@pytest.mark.asyncio
async def test_completed_provider_http_error_releases_slot_without_refunding_attempt(runtime):
    import httpx
    from google.genai.errors import ServerError
    response = httpx.Response(504, json={'error': {'code': 504, 'message': 'private provider detail'}})
    assert response.is_closed
    service.generate.side_effect = ServerError(504, {'error': {'message': 'private provider detail'}}, response=response)
    first = await service.translate('1', '7d')
    assert first['item']['status'] == 'error'
    assert runtime.attempts == 1 and not runtime.held
    assert (await service.translate('1', '7d'))['item']['status'] == 'error'
    assert service.generate.await_count == 1


def test_mixed_non_latin_text_is_not_mistaken_for_russian():
    assert CoverResult(status='ready', source_text='Мой 日本語', text_ru='Мой японский язык').text_ru == 'Мой японский язык'


@pytest.mark.asyncio
async def test_reservation_failure_never_calls_model(runtime):
    runtime.reserve = Mock(side_effect=RuntimeError('db down'))
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d')
    assert exc.value.status_code == 503
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_failure_keeps_claim_and_cannot_repeat_paid_call(runtime):
    runtime.finish = Mock(side_effect=RuntimeError('db down'))
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d')
    assert exc.value.status_code == 503
    assert (await service.translate('1', '14d'))['item']['status'] == 'running'
    assert service.generate.await_count == runtime.attempts == 1
    assert runtime.held == {'1'}


@pytest.mark.asyncio
async def test_cancellation_preserves_reservation_without_automatic_repeat(runtime):
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    service.generate.side_effect = blocked
    task = asyncio.create_task(service.translate('1', '7d'))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service.translate('1', '7d'))['item']['status'] == 'running'
    assert runtime.held == {'1'} and runtime.attempts == 1
    assert service.generate.await_count == 1


@pytest.mark.asyncio
async def test_get_budget_exhaustion_keeps_cached_results_and_never_starts_model(runtime):
    runtime.mode = 'live'
    runtime.attempts = 500
    runtime.rows['1'] = {'status': 'ready', 'source_text': 'Title', 'text_ru': 'Заголовок'}
    runtime.rows['2'] = {'status': 'unreadable'}
    result = await service.read(['1', '2', '3'])
    assert [value['status'] for value in result['items']] == ['ready', 'unreadable', 'limit_reached']
    assert result['items'][0]['retry_after'] is None
    assert result['items'][2]['retry_after'] == runtime.reset_at
    assert result['budget'] == {'mode': 'live', 'pilot_limit': 20, 'daily_limit': 500, 'reset_at': runtime.reset_at,
                                'attempts': 500, 'daily_attempts': 500, 'active_slots': 0, 'available_slots': 2}
    assert result['budget']['attempts'] == 500
    service.generate.assert_not_awaited()
    service.download_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_pilot_limit_is_not_reported_as_recognition_error(runtime):
    runtime.attempts = 20
    result = await service.read(['1'])
    assert result['items'][0]['status'] == 'limit_reached'
    assert result['items'][0]['retry_after'] is None
    assert (await service.translate('1', '7d'))['item']['retry_after'] is None
    service.generate.assert_not_awaited()
    service.download_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_limit_post_skips_image_auth_and_model(runtime):
    runtime.mode = 'live'
    runtime.attempts = 500
    response = await service.translate('1', '7d', oidc_token='fake-request-token')
    assert response['item']['status'] == 'limit_reached'
    assert response['item']['retry_after'] == runtime.reset_at
    service.download_image.assert_not_awaited()
    service.create_vertex_client.assert_not_called()
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollover_read_returns_missing_before_post(runtime):
    runtime.mode = 'live'
    runtime.attempts = 500
    runtime.daily_attempts = 0
    result = await service.read(['1'])
    assert result['items'][0]['status'] == 'missing'
    assert result['items'][0]['retry_after'] is None
    assert (await service.translate('1', '7d'))['item']['status'] == 'ready'
    assert runtime.attempts == 501
    service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_oidc_token_passed_only_to_factory(runtime):
    result = await service.translate('1', '7d', oidc_token='fake-request-token')
    service.create_vertex_client.assert_called_once_with(oidc_token='fake-request-token')
    assert 'fake-request-token' not in str(result)
    assert 'fake-request-token' not in str(runtime.rows)


@pytest.mark.asyncio
async def test_auth_refresh_failure_before_reservation_is_sanitized(runtime):
    service.create_vertex_client.side_effect = RuntimeError('fake-request-token secret credential')
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d', oidc_token='fake-request-token')
    assert exc.value.status_code == 503
    assert 'fake-request-token' not in exc.value.detail
    assert runtime.attempts == 0 and not runtime.held
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_metadata_failure_is_fail_closed(runtime):
    runtime.budget = Mock(side_effect=RuntimeError('db down'))
    with pytest.raises(HTTPException) as exc:
        await service.translate('1', '7d')
    assert exc.value.status_code == 503
    service.generate.assert_not_awaited()
    service.download_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_normalized_image_is_default_and_revision_does_not_change_cache(runtime, monkeypatch):
    assert CoverSettings(_env_file=None).image_normalization == 'jpeg2048'
    normalize = Mock(return_value=(b'jpg', 'image/jpeg'))
    monkeypatch.setattr(service, 'normalize_image', normalize)
    runtime.finish = Mock(wraps=runtime.finish)
    await service.translate('1', '7d')
    service.generate.assert_awaited_once_with(service.create_vertex_client.return_value, b'jpg', 'image/jpeg')
    normalize.assert_called_once_with(b'jpg', 'image/jpeg')
    assert runtime.finish.call_args.args[3] == {
        'total_token_count': 8, 'prompt_revision': service.PROMPT_REVISION,
        'image_preprocessing': 'jpeg2048', 'source_kind': 'media_url', 'source_failures': []}
    await service.translate('1', '14d')
    assert service.generate.await_count == 1 and runtime.attempts == 1


@pytest.mark.asyncio
async def test_explicit_normalization_submits_and_hashes_transformed_image(runtime, monkeypatch):
    import hashlib
    monkeypatch.setattr(service, 'get_settings', lambda: SimpleNamespace(
        enabled=True, vertex_model='model', image_normalization='jpeg2048'))
    normalize = Mock(return_value=(b'normalized-jpeg', 'image/jpeg'))
    monkeypatch.setattr(service, 'normalize_image', normalize)
    runtime.reserve = Mock(wraps=runtime.reserve)
    runtime.finish = Mock(wraps=runtime.finish)
    await service.translate('1', '7d')
    normalize.assert_called_once_with(b'jpg', 'image/jpeg')
    runtime.reserve.assert_called_once_with('1', 'model', hashlib.sha256(b'normalized-jpeg').hexdigest())
    service.generate.assert_awaited_once_with(
        service.create_vertex_client.return_value, b'normalized-jpeg', 'image/jpeg')
    assert runtime.finish.call_args.args[3]['image_preprocessing'] == 'jpeg2048'
    assert runtime.attempts == 1


@pytest.mark.asyncio
async def test_normalization_failure_precedes_auth_and_paid_reservation(runtime, monkeypatch):
    monkeypatch.setattr(service, 'get_settings', lambda: SimpleNamespace(
        enabled=True, vertex_model='model', image_normalization='jpeg2048'))
    monkeypatch.setattr(service, 'normalize_image', Mock(side_effect=service.ImageUnavailable()))
    assert (await service.translate('1', '7d'))['item']['status'] == 'image_unavailable'
    service.create_vertex_client.assert_not_called()
    service.generate.assert_not_awaited()
    assert runtime.attempts == 0


@pytest.mark.asyncio
async def test_legacy_equal_cache_is_neutral_without_image_auth_or_model(runtime):
    runtime.rows['1'] = {'status': 'ready', 'source_text': 'Как сохранить текст?', 'text_ru': ' Как сохранить текст? ',
                         'attempt_recorded': True}
    result = await service.translate('1', '7d')
    assert result['item']['status'] == 'not_needed' and result['item']['text_ru'] == ''
    assert result['item']['source_text'] == 'Как сохранить текст?'
    assert result['item']['reason_code'] == 'unchanged_source'
    assert result['attempt_reserved'] is False and result['item']['attempt_recorded'] is True
    assert runtime.rows['1']['status'] == 'ready'
    service.download_image.assert_not_awaited()
    service.create_vertex_client.assert_not_called()
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_sources_fail_over_before_one_reservation(runtime):
    service.carousels.detail.return_value = {'media_url': 'https://x.fbcdn.net/expired',
        'thumbnail_url': 'https://x.fbcdn.net/thumb', 'shortcode': 'Abc123_'}
    service.download_image.side_effect = [service.ImageUnavailable('expired_signature'),
        service.ImageUnavailable('timeout'), (b'jpg', 'image/jpeg')]
    runtime.finish = Mock(wraps=runtime.finish)
    result = await service.translate('1', '7d')
    assert result['attempt_reserved'] is True and runtime.attempts == 1
    assert [call.args[0] for call in service.download_image.await_args_list] == [
        'https://x.fbcdn.net/expired','https://x.fbcdn.net/thumb',
        'https://www.instagram.com/p/Abc123_/media/?size=l']
    assert runtime.finish.call_args.args[3]['source_kind'] == 'public_first_cover'
    assert runtime.finish.call_args.args[3]['source_failures'] == [
        {'source_kind': 'media_url', 'reason_code': 'expired_signature'},
        {'source_kind': 'thumbnail_url', 'reason_code': 'timeout'}]
    service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_russian_image_uses_single_combined_request(runtime):
    service.generate.return_value = ({'status': 'not_needed', 'source_text': 'Мой текст', 'text_ru': ''}, {})
    result = await service.translate('1','7d')
    assert result['item']['status'] == 'not_needed'
    assert result['attempt_reserved'] is True and runtime.attempts == 1
    service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_failure_is_safe_and_never_reserves(runtime):
    service.download_image.side_effect = service.ImageUnavailable('timeout')
    result = await service.translate('1','7d')
    assert result['attempt_reserved'] is False and result['item']['attempt_recorded'] is False
    assert result['item']['reason_code'] == 'timeout'
    assert runtime.attempts == 0
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('header,minimum,maximum', [('120',119,121), ('999999',299,301), ('invalid',59,61)])
async def test_completed_429_reports_bounded_pause_without_refund(runtime, header, minimum, maximum):
    import httpx
    from datetime import datetime, timezone
    from google.genai.errors import ClientError
    response = httpx.Response(429, headers={'Retry-After': header}, json={'error': {'code':429}})
    service.generate.side_effect = ClientError(429, {'error': {'message': 'private detail'}}, response=response)
    runtime.finish = Mock(wraps=runtime.finish)
    result = await service.translate('1','7d')
    assert result['attempt_reserved'] and runtime.attempts == 1 and not runtime.held
    usage = runtime.finish.call_args.args[3]
    seconds = (datetime.fromisoformat(usage['retry_after'])-datetime.now(timezone.utc)).total_seconds()
    assert minimum <= seconds <= maximum
    assert usage['provider_response_complete'] is True and usage['provider_status'] == 429



@pytest.mark.asyncio
async def test_meta_override_precedes_historical_sources_without_exposing_urls(runtime):
    runtime.read_source_override = Mock(return_value={'media_id': '1',
        'media_url': 'https://x.fbcdn.net/refreshed?signature=private',
        'thumbnail_url': 'https://x.fbcdn.net/refreshed-thumb'})
    result = await service.translate('1', '7d')
    service.download_image.assert_awaited_once_with('https://x.fbcdn.net/refreshed?signature=private')
    runtime.read_source_override.assert_called_once_with('1')
    assert result['attempt_reserved'] is True and runtime.attempts == 1
    assert 'private' not in str(result) and 'signature' not in str(result)


@pytest.mark.asyncio
async def test_meta_override_failure_falls_back_to_original_sources(runtime):
    runtime.read_source_override = Mock(return_value={'media_id': '1',
        'media_url': 'https://x.fbcdn.net/refreshed', 'thumbnail_url': 'https://x.fbcdn.net/refreshed-thumb'})
    service.download_image.side_effect = [service.ImageUnavailable('expired_signature'),
        service.ImageUnavailable('timeout'), (b'jpg','image/jpeg')]
    await service.translate('1','7d')
    assert [call.args[0] for call in service.download_image.await_args_list] == [
        'https://x.fbcdn.net/refreshed','https://x.fbcdn.net/refreshed-thumb','https://x.fbcdn.net/a']
    assert runtime.attempts == 1


@pytest.mark.asyncio
async def test_override_for_other_id_is_ignored(runtime):
    runtime.read_source_override = Mock(return_value={'media_id':'2', 'media_url':'https://x.fbcdn.net/other'})
    await service.translate('1','7d')
    service.download_image.assert_awaited_once_with('https://x.fbcdn.net/a')


@pytest.mark.asyncio
async def test_ready_cache_never_reads_source_override(runtime):
    runtime.rows['1'] = {'status':'ready','source_text':'Title','text_ru':'Заголовок'}
    runtime.read_source_override = Mock(side_effect=AssertionError('Готовый cache не требует refresh'))
    result = await service.translate('1','7d')
    assert result['item']['status'] == 'ready' and result['attempt_reserved'] is False
    runtime.read_source_override.assert_not_called()


@pytest.mark.asyncio
async def test_override_store_failure_is_sanitized_before_paid_reservation(runtime):
    runtime.read_source_override = Mock(side_effect=RuntimeError('private signed URL'))
    with pytest.raises(HTTPException) as error:
        await service.translate('1','7d')
    assert error.value.status_code == 503 and 'private' not in error.value.detail
    assert runtime.attempts == 0
    service.download_image.assert_not_awaited()
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_override_cannot_bypass_existing_download_allowlist(runtime):
    from app.services.carousel_cover_image import download_image
    runtime.read_source_override = Mock(return_value={'media_id':'1', 'media_url':'https://127.0.0.1/private'})
    async def guarded(url):
        if '127.0.0.1' in url:
            return await download_image(url)
        return b'jpg', 'image/jpeg'
    service.download_image.side_effect = guarded
    runtime.finish = Mock(wraps=runtime.finish)
    await service.translate('1','7d')
    assert runtime.finish.call_args.args[3]['source_failures'] == [
        {'source_kind':'media_url','reason_code':'unsupported'}]
    assert runtime.attempts == 1



@pytest.mark.parametrize('source,text_ru', [
    ('Как да постигнем успех','Как добиться успеха'),
    ('Мама любить читати книги','Мама любит читать книги'),
    ('Како да направите добар план','Как составить хороший план'),
])
@pytest.mark.asyncio
async def test_cyrillic_foreign_cache_keeps_existing_translation(runtime, source, text_ru):
    runtime.rows['1'] = {'status':'ready','source_text':source,'text_ru':text_ru}
    result = await service.translate('1','7d')
    assert result['item']['status'] == 'ready' and result['item']['text_ru'] == text_ru
    assert result['item']['source_text'] == source
    assert result['attempt_reserved'] is False
    service.generate.assert_not_awaited()
    service.download_image.assert_not_awaited()


@pytest.mark.parametrize('source', ['Как да постигнем успех','Мама любить читати книги',
    'Како да направите добар план','Мой русский текст'])
def test_new_ready_is_never_forced_to_not_needed_by_alphabet(source):
    result = CoverResult(status='ready',source_text=source,text_ru=source)
    assert result.status == 'ready' and result.text_ru == source


@pytest.mark.asyncio
async def test_new_v5_ready_cache_remains_ready_even_when_text_matches(runtime):
    runtime.rows['1'] = {'status':'ready','source_text':'Мой текст','text_ru':'Мой текст',
                         'usage':{'prompt_revision':service.PROMPT_REVISION}}
    result = await service.read(['1'])
    assert result['items'][0]['status'] == 'ready' and result['items'][0]['text_ru'] == 'Мой текст'
    service.generate.assert_not_awaited()
