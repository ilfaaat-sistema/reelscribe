from __future__ import annotations

from typing import Optional

from supabase import Client, create_client

from app.core.config import settings

_client: Optional[Client] = None


def get_db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _client
