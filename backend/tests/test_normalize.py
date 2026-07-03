from __future__ import annotations

import json

from app.pipeline.normalize import ParsedReel, parse_links, parse_message_json


def test_parse_links_reel():
    text = "смотри вот тут https://www.instagram.com/reel/CxYz123_-/ прикольно"
    out = parse_links(text)
    assert out == [ParsedReel(shortcode="CxYz123_-", type="reel", url="https://www.instagram.com/reel/CxYz123_-/")]


def test_parse_links_mixed_types_and_garbage():
    text = """
    мусор мусор instagram.com без слэша
    https://www.instagram.com/reel/AAA111/ some text
    случайный текст без ссылок
    instagram.com/p/BBB222/?utm_source=ig_web
    check this out instagram.com/tv/CCC333
    not-a-link.com/reel/ZZZ999/
    """
    out = parse_links(text)
    by_sc = {r.shortcode: r for r in out}
    assert set(by_sc) == {"AAA111", "BBB222", "CCC333"}
    assert by_sc["AAA111"].type == "reel"
    assert by_sc["BBB222"].type == "post"
    assert by_sc["CCC333"].type == "tv"
    assert "ZZZ999" not in by_sc


def test_parse_links_dedup_by_shortcode():
    text = (
        "https://www.instagram.com/reel/DUP001/\n"
        "https://instagram.com/reel/DUP001/?igshid=abc\n"
    )
    out = parse_links(text)
    assert len(out) == 1
    assert out[0].shortcode == "DUP001"


def test_parse_links_no_matches():
    assert parse_links("тут вообще нет ссылок, просто текст") == []


def test_parse_message_json_basic():
    payload = {
        "messages": [
            {"link": "https://www.instagram.com/reel/JSON001/", "text": "hi"},
            {"nested": {"url": "https://www.instagram.com/p/JSON002/"}},
            {"media_url": "https://www.instagram.com/reel/JSON003/", "other": "field"},
            {"ignored_field": "https://www.instagram.com/reel/NOPE/"},
        ]
    }
    raw = json.dumps(payload).encode("utf-8")
    out = parse_message_json(raw)
    scs = {r.shortcode for r in out}
    assert scs == {"JSON001", "JSON002", "JSON003"}


def test_parse_message_json_invalid_json_falls_back_to_text_scan():
    raw = b"not json but has https://www.instagram.com/reel/FALLBACK1/ inside"
    out = parse_message_json(raw)
    assert [r.shortcode for r in out] == ["FALLBACK1"]


def test_parse_message_json_mojibake():
    # UTF-8 bytes for a Cyrillic string, mis-decoded as latin-1 then re-encoded as utf-8,
    # simulating an export that double-encoded text around a valid link.
    original = "текст со ссылкой https://www.instagram.com/reel/MOJI001/ конец"
    mojibake = original.encode("utf-8").decode("latin-1")
    raw = mojibake.encode("utf-8")
    out = parse_message_json(raw)
    assert [r.shortcode for r in out] == ["MOJI001"]
