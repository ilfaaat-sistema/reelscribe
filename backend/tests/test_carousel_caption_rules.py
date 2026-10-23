"""Правила подписи не требуют настроек приложения и провайдерских библиотек."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.carousel_caption_rules import classify_caption, needs_caption_translation, source_hash


@pytest.mark.parametrize('text,category', [
    (None, 'empty'), ('', 'empty'), (' \n\t', 'empty'),
    ('123 🎉 — !!!', 'not_needed'), ('#fitness #workout @hello.world', 'not_needed'),
    ('https://example.com/english-text', 'not_needed'), ('www.example.com', 'not_needed'),
    ('some.handle', 'not_needed'), ('some_handle', 'not_needed'), ('a.b_c', 'not_needed'),
    ('_hello', 'not_needed'), ('hello_', 'not_needed'), ('._hello', 'not_needed'),
    ('❤️ vozik.a', 'not_needed'), ('Read vozik.a ❤️', 'eligible'),
    ('Русская подпись #english #fitness @hello.world https://foreign.example', 'not_needed'),
    ('Hello #русский #текст @русский.ник', 'eligible'),
    ('OK 🎉', 'eligible'), ('Go', 'eligible'), ('a', 'eligible'), ('Hello.', 'eligible'),
    ('你好', 'eligible'), ('مرحبا', 'eligible'), ('Ж', 'not_needed'),
    ('аааabcdefg', 'not_needed'), ('ааabcdefgh', 'eligible'),
    ('Read more. #workout @hello.world https://example.com', 'eligible'),
    ('x' * 12000, 'eligible'), ('x' * 12001, 'too_long'),
])
def test_caption_categories(text, category):
    assert classify_caption(text) == category
    if category not in ('empty', 'too_long'):
        assert needs_caption_translation(text) == (category == 'eligible')


def test_hash_uses_exact_utf8_without_normalization():
    samples = ['', 'Hello', ' Hello\n', 'é', 'e\u0301', 'Привет 🎉']
    assert len(set(map(source_hash, samples))) == len(samples)
    for sample in samples:
        assert source_hash(sample) == hashlib.sha256(sample.encode('utf-8')).hexdigest()
    assert source_hash(None) == source_hash('')


def test_rules_import_with_standard_library_only():
    backend = Path(__file__).resolve().parents[1]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from app.services.carousel_caption_rules import classify_caption
assert classify_caption('Go') == 'eligible'
assert not any(name.split('.')[0] in {'fastapi', 'google', 'supabase', 'pydantic', 'deepl'} for name in sys.modules)
"""
    subprocess.run([sys.executable, '-S', '-c', script, str(backend)],
                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, check=True)
