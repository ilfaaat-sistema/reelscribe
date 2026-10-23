"""Правила подписи не требуют настроек приложения и провайдерских библиотек."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.services.carousel_caption_rules import (
    classify_caption,
    needs_caption_translation,
    source_hash,
)


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


@pytest.mark.parametrize('source,expected', [
    ('ashrapov_67 🥇🌍\n\n📷 UWW\n\nkazakhstan_wrestling', False),
    ('Read this ashrapov_67\n📷 UWW\nname.handle', True),
    ('Тренер: adilalzhikeev\naitbekov_aas\n📷 UWW\nkazakhstan_wrestling', False),
    ('бапкері: adilalzhikeev\naitbekov_aas\n📷 UWW\nkazakhstan_wrestling', True),
    ('Шүкір🤍', True), ('Қазақша мәтін', True), ('Русский текст полностью', False),
    ('Русский текст\n📷 Best picture in town', True),
    ('📍Dostyk Plaza\n📍Moskva Metropolitan\nДля заказа Direct/WhatsApp', False),
    ('📍Grand Plaza\nПриходите к нам', False),
    ('📍Grand Plaza', True),
    ('📍Best tacos in town', True),
    ('📍Best tacos in town\nДля заказа обращайтесь к нам каждый день', True),
    ('📍Grand Plaza\nBest tacos in town\nДля заказа обращайтесь к нам каждый день', True),
    ('📍Grand Plaza\nКупите сейчас\nBuy today', True),
    ('Нашли заведения в Казани\n📍sea.salt.bistro\n📍tshkzn\n📍mestotruda\nПишите в комментарии', False),
])
def test_version_two_metadata_and_short_cyrillic_policy(source, expected):
    assert needs_caption_translation(source) is expected


def test_rules_version_is_public_and_hash_stays_exact_with_ignored_metadata():
    from app.services.carousel_caption_rules import CAPTION_RULES_VERSION
    assert CAPTION_RULES_VERSION == 2
    source = '📍Grand Plaza\nРусский текст\n📷 UWW\nname.handle'
    assert source_hash(source) == hashlib.sha256(source.encode('utf-8')).hexdigest()
    assert source_hash(source) != source_hash('Русский текст')


@pytest.mark.parametrize('source,expected', [
    ('«Шүкір»', True), ('«Love you»', True), ('“Шүкір”', True), ('"Шүкір"', True),
    ('Сегодня наградили участников проекта «Халықаралық бизнес». Подробнее в карусели.', False),
    ('Сегодня показали фильм «Жеті қарақшы».', False),
    ('Награда «Алтын белгі» получена выпускницей.', False),
    ('Сегодня мы обсуждаем «Love you».', False),
    ('«Шүкір»\nҚазақша мәтін', True),
    ('Встреча с Айнұр Темірханқызы.', True),
])
def test_paired_titles_do_not_hide_foreign_standalone_text(source, expected):
    assert needs_caption_translation(source) is expected


@pytest.mark.parametrize('credit', ['📷 UWW', '📸 imbelovandrey', '📸 : JUCY_xcom / X'])
def test_short_camera_credit_does_not_force_russian_caption(credit):
    assert not needs_caption_translation('Русский текст для читателя\n' + credit)
