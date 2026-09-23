from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_CHUNK = 4500  # Google/DeepL chunk limit

# Правило «нужно ли переводить» (ТЗ 07 §3), одно на пайплайн и бэкфилл: доля кириллических
# букв среди буквенных символов ниже порога при не менее чем _MIN_LETTERS буквах. Короткие
# подписи из одних хэштегов/эмодзи (букв меньше порога) сигнала не дают — не переводим.
_LETTER_RE = re.compile(r'[^\W\d_]', re.UNICODE)
_CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')
_MIN_LETTERS = 12
_CYRILLIC_RATIO_THRESHOLD = 0.3


def needs_translation(text: str | None) -> bool:
    """Нужно ли переводить текст на русский — по доле кириллических букв среди буквенных.

    Используется и для расшифровок, и для подписи поста (caption): язык подписи не совпадает
    с языком речи, поэтому решение принимается по самому тексту, а не по transcripts.language.
    """
    if not text:
        return False
    letters = _LETTER_RE.findall(text)
    if len(letters) < _MIN_LETTERS:
        return False
    cyrillic = sum(1 for ch in letters if _CYRILLIC_RE.match(ch))
    return (cyrillic / len(letters)) < _CYRILLIC_RATIO_THRESHOLD


def translate_to_ru(text: str) -> str:
    """DeepL если есть DEEPL_API_KEY в env, иначе Google (бесплатно, без ключа)."""
    if not text or not text.strip():
        return text
    from app.core.config import settings
    chunks = _split(text.strip(), _MAX_CHUNK)
    if settings.deepl_api_key:
        return _deepl(chunks, settings.deepl_api_key)
    return _google(chunks)


def _google(chunks: list[str]) -> str:
    from deep_translator import GoogleTranslator
    parts: list[str] = []
    for chunk in chunks:
        try:
            parts.append(GoogleTranslator(source='auto', target='ru').translate(chunk) or chunk)
        except Exception as exc:
            logger.warning('Google Translate chunk failed: %s', exc)
            parts.append(chunk)
    return ' '.join(parts)


def _deepl(chunks: list[str], api_key: str) -> str:
    from deep_translator import DeepLTranslator
    parts: list[str] = []
    for chunk in chunks:
        try:
            parts.append(DeepLTranslator(api_key=api_key, source='auto', target='RU').translate(chunk) or chunk)
        except Exception as exc:
            logger.warning('DeepL chunk failed (%s), fallback to Google', exc)
            parts.append(_google([chunk]))
    return ' '.join(parts)


def _split(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts
