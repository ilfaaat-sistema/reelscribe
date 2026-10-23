"""Чистые правила подписей: общие для API и фоновой очереди без SDK и настроек."""
from __future__ import annotations

import hashlib
import re

MAX_SOURCE_LENGTH = 12_000
_URL = re.compile(r'\b(?:https?://|www\.)\S+', re.IGNORECASE)
_TAG = re.compile(r'(?<!\w)[@#][\w.]+', re.UNICODE)
_HANDLE = re.compile(r'(?=[\w.]*[._])[\w.]*\w', re.UNICODE)
_LETTER = re.compile(r'[^\W\d_]', re.UNICODE)
_CYRILLIC = re.compile(r'[а-яёА-ЯЁ]')


def source_hash(text: str | None) -> str:
    """Хэш точных исходных UTF-8 байтов, включая пробелы и форму Unicode."""
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def needs_caption_translation(text: str | None) -> bool:
    """Решение по буквам обычного текста без ссылок, упоминаний и хэштегов."""
    if not text:
        return False
    body = _TAG.sub(' ', _URL.sub(' ', text)).strip()
    # Краевые эмодзи/пунктуация не превращают одиночный handle в обычный текст.
    handle_body = re.sub(r'^\W+|\W+$', '', body)
    if _HANDLE.fullmatch(handle_body):
        return False
    letters = _LETTER.findall(body)
    if not letters:
        return False
    return sum(bool(_CYRILLIC.fullmatch(char)) for char in letters) / len(letters) < 0.3


def classify_caption(text: str | None) -> str:
    """Категория исходника для инвентаризации и проверки до вызова модели."""
    if not text or not text.strip():
        return 'empty'
    if len(text) > MAX_SOURCE_LENGTH:
        return 'too_long'
    return 'eligible' if needs_caption_translation(text) else 'not_needed'
