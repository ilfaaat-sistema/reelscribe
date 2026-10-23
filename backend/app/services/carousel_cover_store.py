"""Привилегированное хранилище обложек, отдельное от общего anon-клиента."""
from datetime import datetime, timedelta
from functools import lru_cache

from app.core.config import settings

PROMPT_VERSION = 'cover-v1'
TABLE = 'carousel_cover_translations'


@lru_cache
def get_cover_db():
    if not settings.supabase_service_role_key or not settings.supabase_url:
        raise RuntimeError('Хранилище обложек не настроено')
    from supabase import ClientOptions, create_client
    return create_client(settings.supabase_url, settings.supabase_service_role_key,
                         options=ClientOptions(postgrest_client_timeout=10))


def _rpc(name, params):
    data = get_cover_db().rpc(name, params).execute().data
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or not isinstance(data.get('status'), str):
        raise TypeError('Некорректный ответ хранилища обложек')
    return data


class CoverStore:
    def budget(self):
        data = _rpc('carousel_cover_budget_state', {})
        if (data.get('mode') not in ('pilot', 'live') or data.get('pilot_limit') != 20
                or type(data.get('daily_limit')) is not int or data['daily_limit'] not in (500, 2500)
                or data['status'] not in ('available', 'limit_reached')):
            raise ValueError('Некорректные ограничения бюджета обложек')
        reset = datetime.fromisoformat(data['reset_at'].replace('Z', '+00:00'))
        if reset.utcoffset() != timedelta(0):
            raise ValueError('Граница бюджета должна быть в UTC')
        expected_retry = data['reset_at'] if data['mode'] == 'live' and data['status'] == 'limit_reached' else None
        if data.get('retry_after') != expected_retry:
            raise ValueError('Некорректный срок ожидания бюджета обложек')
        return data

    def read(self, ids):
        return (get_cover_db().table(TABLE).select('media_id,status,source_text,text_ru,created_at')
                .eq('prompt_version', PROMPT_VERSION).in_('media_id', ids).execute().data or [])

    def reserve(self, media_id, model, image_hash):
        return _rpc('reserve_carousel_cover', {'p_id': media_id, 'p_version': PROMPT_VERSION,
                    'p_model': model, 'p_hash': image_hash})

    def unavailable(self, media_id):
        return _rpc('unavailable_carousel_cover', {'p_id': media_id, 'p_version': PROMPT_VERSION})

    def finish(self, media_id, token, result, usage, duration_ms, uncertain=False):
        return _rpc('finish_carousel_cover', {'p_id': media_id, 'p_version': PROMPT_VERSION,
                    'p_token': token, 'p_status': result['status'],
                    'p_source': result.get('source_text'), 'p_text': result.get('text_ru'),
                    'p_usage': usage, 'p_duration_ms': duration_ms, 'p_uncertain': uncertain})
