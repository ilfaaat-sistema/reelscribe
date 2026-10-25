"""Представление источников Reels и избранных без записи в платную очередь Reels."""
from __future__ import annotations

from typing import Any


def merge_sources(result: dict[str, Any], authors: list[dict[str, Any]]) -> dict[str, Any]:
    active = {row['username']: dict(row, tracking_reels=True, tracking_carousels=False)
              for row in result['active']}
    excluded = {row['username']: dict(row, tracking_reels=False, tracking_carousels=False)
                for row in result['excluded']}
    for author in authors:
        if not author.get('active', True):
            continue
        username = author['username']
        if username not in active:
            # Исключённый вручную Reels-источник остаётся исключённым: звезда его не восстанавливает.
            origin = excluded.pop(username, {})
            active[username] = {
                'username': username, 'full_name': None, 'followers': None,
                'followers_updated_at': None, 'median_views': None, 'norm_n': 0,
                'norm_reliable': False, 'last_reel_at': None, 'reels_count': None,
                'viral_count': None, 'last_scraped_at': None, 'added_at': None,
                'needs_deep': False, 'tracking_reels': False,
                'source_excluded': bool(origin), 'excluded_at': origin.get('excluded_at'),
            }
        active[username].update(
            tracking_carousels=True, carousel_status=author.get('status'),
            last_checked_at=author.get('last_checked_at'),
            history_status=author.get('history_status'),
        )
    return {**result, 'active': list(active.values()), 'excluded': list(excluded.values())}
