from __future__ import annotations

from typing import Any, Optional


def extract_metadata(info: dict[str, Any]) -> dict[str, Any]:
    return {
        'author_handle': info.get('uploader_id') or info.get('channel_id') or info.get('uploader'),
        'author_followers': info.get('channel_follower_count'),
        'views': info.get('view_count'),
        'likes': info.get('like_count'),
        'comments': info.get('comment_count'),
        'caption': info.get('description'),
        'posted_at': _parse_date(info.get('upload_date')),
    }


def _parse_date(s: Optional[str]) -> Optional[str]:
    if not s or len(s) != 8:
        return None
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
