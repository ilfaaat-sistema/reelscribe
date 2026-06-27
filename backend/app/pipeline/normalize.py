from __future__ import annotations

import json
import re
from dataclasses import dataclass

_RE_URL = re.compile(
    r'instagram\.com/(?P<type>reel|p|tv)/(?P<sc>[A-Za-z0-9_-]+)',
    re.IGNORECASE,
)


@dataclass
class ParsedReel:
    shortcode: str
    type: str   # reel | post | tv
    url: str


def _fix_mojibake(s: str) -> str:
    """Instagram JSON экспорты иногда содержат UTF-8 закодированный как latin-1."""
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def parse_links(text: str) -> list[ParsedReel]:
    seen: dict[str, ParsedReel] = {}
    for m in _RE_URL.finditer(text):
        sc = m.group('sc')
        kind = m.group('type').lower()
        reel_type = 'reel' if kind == 'reel' else ('tv' if kind == 'tv' else 'post')
        url = f'https://www.instagram.com/{kind}/{sc}/'
        seen[sc] = ParsedReel(shortcode=sc, type=reel_type, url=url)
    return list(seen.values())


def parse_message_json(raw: bytes) -> list[ParsedReel]:
    text = raw.decode('utf-8', errors='replace')
    text = _fix_mojibake(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return parse_links(text)

    links: list[str] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ('link', 'url', 'media_url') and isinstance(v, str) and 'instagram.com' in v:
                    links.append(v)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return parse_links('\n'.join(links))
