"""Отдельный привилегированный клиент только для серверного кэша перевода."""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.core.config import settings


@lru_cache(maxsize=1)
def get_translation_db() -> Client:
    if not settings.supabase_service_role_key:
        raise RuntimeError('Серверное хранилище перевода не настроено')
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
