"""Единая точка получения числа подписчиков автора.

Порядок: Apify profile-актор (надёжно, без входа в IG) → instaloader (только если подложена
залогиненная сессия). Используется и воркером (обогащение метрик), и backfill-скриптом.
"""
from __future__ import annotations

from app.core.config import settings
from app.pipeline.apify_client import get_followers_via_apify


async def resolve_followers(username: str | None) -> int | None:
    if not username:
        return None
    if settings.apify_token_list:
        followers = await get_followers_via_apify(username)
        if followers is not None:
            return followers
    if settings.instaloader_enabled and settings.instaloader_session_file:
        from app.pipeline.instaloader_downloader import get_followers_via_instaloader
        return await get_followers_via_instaloader(username)
    return None
