from __future__ import annotations

from app.pipeline.translate import needs_translation


def test_needs_translation_english_caption():
    assert needs_translation("This is a completely English caption about travel and food") is True


def test_needs_translation_russian_caption_not_translated():
    assert needs_translation("Это обычная подпись на русском языке про путешествия") is False


def test_needs_translation_hashtags_and_emoji_only_not_translated():
    # Букв меньше порога (_MIN_LETTERS=12) — сигнала мало, не переводим.
    assert needs_translation("#travel #food 🔥🔥🔥") is False


def test_needs_translation_empty_or_none():
    assert needs_translation(None) is False
    assert needs_translation("") is False
    assert needs_translation("   ") is False


def test_needs_translation_mixed_mostly_english():
    # Немного кириллицы (имя/хэштег), но большинство букв — английские.
    text = "Amazing sunset in Bali, check out this beautiful place #Иван"
    assert needs_translation(text) is True


def test_needs_translation_mixed_mostly_russian():
    text = "Прекрасный закат в Бали, посмотри это красивое место #travel"
    assert needs_translation(text) is False
