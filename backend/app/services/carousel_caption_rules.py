"""Чистые правила подписей: общие для API и фоновой очереди без SDK и настроек."""
from __future__ import annotations

import hashlib
import re
import unicodedata

CAPTION_RULES_VERSION = 2
MAX_SOURCE_LENGTH = 12_000
_URL = re.compile(r'\b(?:https?://|www\.)\S+', re.IGNORECASE)
_TAG = re.compile(r'(?<!\w)[@#][\w.]+', re.UNICODE)
_HANDLE = re.compile(r'(?=[\w.]*[._])[\w.]*\w', re.UNICODE)
_LETTER = re.compile(r'[^\W\d_]', re.UNICODE)
_CYRILLIC = re.compile(r'[а-яёА-ЯЁ]')
_QUOTED = re.compile(r'«[^«»\n]*»|“[^“”\n]*”|"[^"\n]*"')


def source_hash(text: str | None) -> str:
    """Хэш точных исходных UTF-8 байтов, включая пробелы и форму Unicode."""
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _bare_handle(match):
    token = match.group().strip('.')
    return ' ' if _HANDLE.fullmatch(token) else match.group()


def _letters(text):
    return _LETTER.findall(text)


def _non_russian_cyrillic(text):
    return any(not _CYRILLIC.fullmatch(char) and 'CYRILLIC' in unicodedata.name(char, '') for char in _letters(text))


def _russian_context(text):
    lines = [line for line in text.splitlines() if _letters(line)]
    return bool(lines) and any(_CYRILLIC.search(line) for line in lines) and all(
        not _non_russian_cyrillic(line)
        and sum(bool(_CYRILLIC.fullmatch(char)) for char in _letters(line)) / len(_letters(line)) >= 0.3
        for line in lines
    )


def _foreign_body(text):
    letters = _letters(text)
    if not letters:
        return False
    # Название в парных кавычках не меняет язык уверенно русского контекста.
    # Буквы самой цитаты остаются в обычном ratio: отдельное «Love you» требует перевода.
    if _non_russian_cyrillic(text) and not _russian_context(_QUOTED.sub(' ', text)):
        return True
    return sum(bool(_CYRILLIC.fullmatch(char)) for char in letters) / len(letters) < 0.3


def needs_caption_translation(text: str | None) -> bool:
    """Решение по содержательным строкам; исходник и его hash не нормализуются."""
    if not text:
        return False
    body = _TAG.sub(' ', _URL.sub(' ', text))
    body = re.sub(r'[\w.]+', _bare_handle, body)
    lines = []
    venues = []
    for line in body.splitlines():
        line = line.strip()
        # Только короткий credit без обычных слов: аббревиатура либо уже удалённый handle.
        if line.startswith(('📷', '📸')):
            credit = re.sub(r'[\s:/|,]+', ' ', line[1:].lstrip('\ufe0f')).strip()
            if not credit or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,29}', credit):
                continue
            if _foreign_body(line):
                return True
        if line.startswith('📍'):
            location = line.removeprefix('📍').lstrip('\ufe0f').strip()
            if re.fullmatch(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}', location):
                venues.append(line)
                continue
            # Предложение после pin не скрывается большим русским контекстом.
            if len(re.findall(r'[A-Za-z]+', location)) >= 2:
                return True
        lines.append(line)
    ordinary = [line for line in lines if _letters(line)]
    if venues and ordinary:
        # Каждая обычная строка должна пройти русский gate; foreign-реклама блокирует skip venue.
        if any(_foreign_body(line) for line in ordinary):
            return True
        if any(_CYRILLIC.search(line) for line in ordinary):
            venues = []
    return _foreign_body('\n'.join(lines + venues))


def classify_caption(text: str | None) -> str:
    """Категория исходника для инвентаризации и проверки до вызова модели."""
    if not text or not text.strip():
        return 'empty'
    if len(text) > MAX_SOURCE_LENGTH:
        return 'too_long'
    return 'eligible' if needs_caption_translation(text) else 'not_needed'
