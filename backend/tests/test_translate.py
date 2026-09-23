from __future__ import annotations

import pytest

from app.pipeline import translate
from app.pipeline.translate import (
    TranslationFailedError,
    TranslationQuotaExceededError,
    needs_translation,
    translation_failure_reason,
)


@pytest.fixture(autouse=True)
def _reset_deepl_breaker():
    """Предохранитель DeepL — модульный глобал в translate.py, тесты не должны видеть
    его состояние друг у друга."""
    translate._deepl_disabled_reason = None
    yield
    translate._deepl_disabled_reason = None


# ── needs_translation (уже существующее поведение — не трогаем) ─────────────

def test_needs_translation_english_caption():
    assert needs_translation("This is a completely English caption about travel and food") is True


def test_needs_translation_russian_caption_not_translated():
    assert needs_translation("Это обычная подпись на русском языке про путешествия") is False


def test_needs_translation_hashtags_and_emoji_only_not_translated():
    assert needs_translation("#travel #food 🔥🔥🔥") is False


def test_needs_translation_empty_or_none():
    assert needs_translation(None) is False
    assert needs_translation("") is False
    assert needs_translation("   ") is False


def test_needs_translation_mixed_mostly_english():
    text = "Amazing sunset in Bali, check out this beautiful place #Иван"
    assert needs_translation(text) is True


def test_needs_translation_mixed_mostly_russian():
    text = "Прекрасный закат в Бали, посмотри это красивое место #travel"
    assert needs_translation(text) is False


# ── translation_failure_reason: все коды причин ──────────────────────────────

def test_failure_reason_empty():
    assert translation_failure_reason("Hello world", "") == 'empty'
    assert translation_failure_reason("Hello world", "   ") == 'empty'
    assert translation_failure_reason("Hello world", None) == 'empty'


def test_failure_reason_error_marker_google_stub():
    original = "This is a long enough original sentence that needed translation to Russian."
    translated = "Error 500 (Server Error)!!1500.That's an error..."
    assert translation_failure_reason(original, translated) == 'error_marker'


def test_failure_reason_error_marker_typographic_apostrophe():
    original = "This is a long enough original sentence that needed translation to Russian."
    translated = "That’s an error. Please try again later."
    assert translation_failure_reason(original, translated) == 'error_marker'


def test_failure_reason_error_marker_html_tag():
    original = "This is a long enough original sentence that needed translation to Russian."
    translated = "<html><body>Bad gateway</body></html>"
    assert translation_failure_reason(original, translated) == 'error_marker'


def test_failure_reason_too_short():
    original = "x" * 100  # >= 80 символов
    translated = "коротко"  # намного короче 20% от 100
    assert translation_failure_reason(original, translated) == 'too_short'


def test_failure_reason_too_short_not_triggered_for_short_original():
    # Оригинал короче порога (80) — правило too_short не применяется вовсе.
    original = "Hi"
    translated = "П"
    assert translation_failure_reason(original, translated) is None


def test_failure_reason_unchanged():
    original = "This is an English sentence that should have been translated to Russian."
    translated = original
    assert translation_failure_reason(original, translated) == 'unchanged'


def test_failure_reason_no_cyrillic():
    original = "C'est une belle journée ensoleillée"
    translated = "This is a beautiful sunny day right now"
    assert translation_failure_reason(original, translated) == 'no_cyrillic'


def test_failure_reason_ok_short_string_not_garbage():
    # Короткая строка «OK» — needs_translation(original) ложно, unchanged не срабатывает,
    # и сама строка тоже слишком короткая для no_cyrillic.
    assert translation_failure_reason("OK", "OK") is None


def test_failure_reason_hashtags_string_not_garbage():
    original = "#travel #food 🔥🔥🔥"
    translated = "#travel #food 🔥🔥🔥"
    assert translation_failure_reason(original, translated) is None


def test_failure_reason_good_translation():
    original = "This is an English sentence that should have been translated to Russian."
    translated = "Это английское предложение, которое должно было быть переведено на русский."
    assert translation_failure_reason(original, translated) is None


# ── DeepL: квота → TranslationQuotaExceededError, Google НЕ вызывается ───────

def test_deepl_quota_exceeded_raises_and_disables_google_fallback(monkeypatch):
    import deepl

    class FakeTranslator:
        def __init__(self, api_key):
            self.api_key = api_key

        def translate_text(self, text, target_lang):
            raise deepl.QuotaExceededException("quota exceeded for this billing period")

    google_calls = {"n": 0}

    class FakeGoogleTranslator:
        def __init__(self, **kwargs):
            pass

        def translate(self, text):
            google_calls["n"] += 1
            return "не должно вызваться"

    monkeypatch.setattr(deepl, "Translator", FakeTranslator)
    monkeypatch.setattr("deep_translator.GoogleTranslator", FakeGoogleTranslator)

    from app.core.config import settings
    monkeypatch.setattr(settings, "deepl_api_key", "fake-key")

    with pytest.raises(TranslationQuotaExceededError):
        translate.translate_to_ru("Hello, this needs translation.")

    assert google_calls["n"] == 0


def test_deepl_quota_breaker_blocks_subsequent_calls_without_network(monkeypatch):
    import deepl

    call_count = {"n": 0}

    class FakeTranslator:
        def __init__(self, api_key):
            pass

        def translate_text(self, text, target_lang):
            call_count["n"] += 1
            raise deepl.QuotaExceededException("quota exceeded")

    monkeypatch.setattr(deepl, "Translator", FakeTranslator)

    from app.core.config import settings
    monkeypatch.setattr(settings, "deepl_api_key", "fake-key")

    with pytest.raises(TranslationQuotaExceededError):
        translate.translate_to_ru("First call hits the network.")
    assert call_count["n"] == 1

    # Второй вызов в этом же процессе не должен идти в сеть вовсе — предохранитель.
    with pytest.raises(TranslationQuotaExceededError):
        translate.translate_to_ru("Second call must be short-circuited.")
    assert call_count["n"] == 1


# ── DeepL: транзиентная ошибка → фолбэк на Google отработал ──────────────────

def test_deepl_transient_error_falls_back_to_google(monkeypatch):
    import deepl

    class FakeTranslator:
        def __init__(self, api_key):
            pass

        def translate_text(self, text, target_lang):
            raise deepl.DeepLException("temporary server error", should_retry=True)

    class FakeResult:
        def __init__(self, text):
            self.text = text

    class FakeGoogleTranslator:
        def __init__(self, **kwargs):
            pass

        def translate(self, text):
            return "Это переведённый через запасной путь текст, вполне достаточной длины."

    monkeypatch.setattr(deepl, "Translator", FakeTranslator)
    monkeypatch.setattr("deep_translator.GoogleTranslator", FakeGoogleTranslator)

    from app.core.config import settings
    monkeypatch.setattr(settings, "deepl_api_key", "fake-key")

    result = translate.translate_to_ru("Hello, this text needs translation via fallback.")
    assert result == "Это переведённый через запасной путь текст, вполне достаточной длины."


# ── Google: маркер ошибки → TranslationFailedError ───────────────────────────

def test_google_error_marker_raises_translation_failed(monkeypatch):
    class FakeGoogleTranslator:
        def __init__(self, **kwargs):
            pass

        def translate(self, text):
            return "Error 500 (Server Error)!!1500.That's an error..."

    monkeypatch.setattr("deep_translator.GoogleTranslator", FakeGoogleTranslator)

    from app.core.config import settings
    monkeypatch.setattr(settings, "deepl_api_key", "")

    with pytest.raises(TranslationFailedError) as exc_info:
        translate.translate_to_ru("This is a long enough original text to translate now.")
    assert exc_info.value.reason == 'error_marker'


# ── Многокусочный текст: второй кусок битый → исключение целиком, без склейки ─

def test_multi_chunk_bad_second_chunk_raises_without_partial_result(monkeypatch):
    calls: list[str] = []

    class FakeGoogleTranslator:
        def __init__(self, **kwargs):
            pass

        def translate(self, text):
            calls.append(text)
            if len(calls) == 1:
                return "привет мир " * 150  # валидный длинный кусок (>=900 символов)
            return "That's an error. Please try again."  # второй кусок — заглушка

    monkeypatch.setattr("deep_translator.GoogleTranslator", FakeGoogleTranslator)

    from app.core.config import settings
    monkeypatch.setattr(settings, "deepl_api_key", "")

    # 4501 символ → _split режет на 2 куска (4500 + 1), второй кусок «битый».
    long_text = "A" * 4501
    with pytest.raises(TranslationFailedError) as exc_info:
        translate.translate_to_ru(long_text)

    assert exc_info.value.reason == 'error_marker'
    assert len(calls) == 2  # оба куска реально дошли до перевода, склейки при этом не было


# ── run.py: _translate_transcript_safe не бросает исключений наружу ──────────

def test_translate_transcript_safe_swallows_failures(monkeypatch):
    from app.workers.run import _translate_transcript_safe

    def fake_raise_failed(text):
        raise TranslationFailedError('error_marker')

    monkeypatch.setattr("app.pipeline.translate.translate_to_ru", fake_raise_failed)
    assert _translate_transcript_safe("some text", "en") is None


def test_translate_transcript_safe_swallows_quota_exceeded(monkeypatch):
    from app.workers.run import _translate_transcript_safe

    def fake_raise_quota(text):
        raise TranslationQuotaExceededError("quota exceeded")

    monkeypatch.setattr("app.pipeline.translate.translate_to_ru", fake_raise_quota)
    assert _translate_transcript_safe("some text", "en") is None


def test_translate_transcript_safe_returns_translation_on_success(monkeypatch):
    from app.workers.run import _translate_transcript_safe

    monkeypatch.setattr("app.pipeline.translate.translate_to_ru", lambda text: "перевод")
    assert _translate_transcript_safe("some text", "en") == "перевод"
