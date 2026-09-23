from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

_MAX_CHUNK = 4500  # Google/DeepL chunk limit

# Правило «нужно ли переводить» (ТЗ 07 §3), одно на пайплайн и бэкфилл: доля кириллических
# букв среди буквенных символов ниже порога при не менее чем _MIN_LETTERS буквах. Короткие
# подписи из одних хэштегов/эмодзи (букв меньше порога) сигнала не дают — не переводим.
_LETTER_RE = re.compile(r'[^\W\d_]', re.UNICODE)
_CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')
_MIN_LETTERS = 12
_CYRILLIC_RATIO_THRESHOLD = 0.3

# Признаки того, что «перевод» на самом деле — HTML-заглушка об ошибке, которую Google
# иногда отдаёт со статусом 200 (deep_translator считает это успехом и кладёт страницу
# ошибки в результат). Проверяем в нижнем регистре. Апостроф встречается в обоих
# вариантах — обычный ' и типографский ’ (Google меняет их между заглушками).
_ERROR_MARKERS = (
    'error 500 (server error)',
    "that's an error",
    'that’s an error',
    'too many requests',
)
_HTML_TAG_RE = re.compile(r'<\s*[a-zA-Z][a-zA-Z0-9]*[^>]*>')

# Найдено 23.09.2026 на живой базе: из 703 переводов расшифровок 246 содержали именно эту
# заглушку, а 59 дословно повторяли оригинал — оба случая раньше писались в БД как успех.
_TOO_SHORT_MIN_ORIGINAL_LEN = 80
_TOO_SHORT_RATIO = 0.2


class TranslationFailedError(Exception):
    """Перевод получен, но не годится для записи в БД (см. translation_failure_reason)."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


class TranslationQuotaExceededError(TranslationFailedError):
    """Квота DeepL исчерпана — фолбэка на Google для этого случая быть не должно."""

    def __init__(self, message: str | None = None):
        super().__init__('quota_exceeded', message)


# Предохранитель на процесс: после первого QuotaExceeded/Authorization от DeepL в рамках
# одного прогона (воркер/бэкфилл) остальные вызовы этого процесса даже не ходят в сеть —
# квота или ключ не восстановятся за секунды, а долбить API впустую незачем.
_deepl_disabled_reason: str | None = None


def translation_failure_reason(original: str | None, translated: str | None) -> str | None:
    """Проверка результата перевода на пригодность для записи в БД.

    Возвращает None, если перевод выглядит нормальным, иначе один из кодов причины:
    empty | error_marker | too_short | no_cyrillic | unchanged.

    empty, error_marker, too_short, no_cyrillic — жёсткие: такой результат не пишем.
    unchanged — мягкая: не блокирует запись, но обязательно логируется предупреждением
    (см. вызывающий код), потому что по факту на живой базе это оказалось систематическим
    браком (458 из 458 переводов подписей).
    """
    if not translated or not translated.strip():
        return 'empty'

    lowered = translated.lower()
    if any(marker in lowered for marker in _ERROR_MARKERS) or _HTML_TAG_RE.search(translated):
        return 'error_marker'

    original = original or ''
    if len(original) >= _TOO_SHORT_MIN_ORIGINAL_LEN and len(translated) < len(original) * _TOO_SHORT_RATIO:
        return 'too_short'

    # unchanged — сравниваем дословно, но только если оригинал сам похож на текст,
    # который стоило переводить (needs_translation): иначе короткие «OK»/хэштеги ложно
    # считались бы браком, хотя переводить их и не нужно было.
    if translated.strip() == original.strip() and needs_translation(original):
        return 'unchanged'

    # no_cyrillic — самый сильный универсальный признак брака: результат сам выглядит так,
    # будто его ещё нужно переводить на русский.
    if needs_translation(translated):
        return 'no_cyrillic'

    return None


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
    """DeepL если есть DEEPL_API_KEY в env, иначе Google (бесплатно, без ключа).

    Бросает TranslationFailedError (или TranslationQuotaExceededError), если пригодный
    перевод получить не удалось — ни при каком исходе исходный текст не возвращается молча.
    Если текст режется на несколько кусков и хотя бы один непригоден — исключение летит
    целиком, частичный (наполовину переведённый) результат не склеивается и не пишется.
    """
    if not text or not text.strip():
        return text
    from app.core.config import settings
    chunks = _split(text.strip(), _MAX_CHUNK)
    if settings.deepl_api_key:
        return _deepl(chunks, settings.deepl_api_key)
    return _google(chunks)


def _validate_chunk(original: str, translated: str) -> str:
    reason = translation_failure_reason(original, translated)
    if reason == 'unchanged':
        logger.warning('Перевод дословно совпал с оригиналом (unchanged): %.80s…', original)
        return translated
    if reason is not None:
        raise TranslationFailedError(reason, f'перевод не прошёл проверку ({reason}): {original[:80]!r}')
    return translated


def _google(chunks: list[str]) -> str:
    """2 попытки с нарастающей паузой на кусок (по образцу apify_client.py). Непригодный
    результат — исключение наружу, никакого «проглатывания» ошибки исходным текстом."""
    from deep_translator import GoogleTranslator
    parts: list[str] = []
    for chunk in chunks:
        last_exc: Exception | None = None
        translated: str | None = None
        for attempt in range(2):
            try:
                translated = GoogleTranslator(source='auto', target='ru').translate(chunk)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — сетевой сбой конкретной попытки
                last_exc = exc
                logger.warning(
                    'Google Translate: попытка %d/2 не удалась (%s)', attempt + 1, exc,
                )
                if attempt < 1:
                    time.sleep(2.0 * (attempt + 1))
        if last_exc is not None:
            raise TranslationFailedError('empty', f'Google Translate не ответил: {last_exc}') from last_exc
        parts.append(_validate_chunk(chunk, translated or ''))
    return ' '.join(parts)


def _deepl(chunks: list[str], api_key: str) -> str:
    """Официальный пакет deepl (не deep_translator — см. ТЗ: только у него отличимы квота/сеть).

    При исчерпании квоты или неверном ключе — TranslationQuotaExceededError и предохранитель
    на остаток процесса (без фолбэка на Google, чтобы переход на DeepL не терялся в первый же
    день). При прочих ошибках DeepL — предупреждение в лог и фолбэк на Google для этого куска.
    """
    global _deepl_disabled_reason
    import deepl

    if _deepl_disabled_reason is not None:
        raise TranslationQuotaExceededError(_deepl_disabled_reason)

    translator = deepl.Translator(api_key)
    parts: list[str] = []
    for chunk in chunks:
        try:
            result = translator.translate_text(chunk, target_lang='RU')
            translated = result.text if not isinstance(result, list) else ' '.join(r.text for r in result)
        except (deepl.QuotaExceededException, deepl.AuthorizationException) as exc:
            _deepl_disabled_reason = f'{type(exc).__name__}: {exc}'
            logger.error('DeepL: %s — дальше в этом прогоне DeepL не используется', _deepl_disabled_reason)
            raise TranslationQuotaExceededError(_deepl_disabled_reason) from exc
        except deepl.DeepLException as exc:
            logger.warning('DeepL chunk failed (%s), fallback to Google', exc)
            parts.append(_google([chunk]))  # _google уже валидирует и бросает при браке
            continue
        parts.append(_validate_chunk(chunk, translated))
    return ' '.join(parts)


def _split(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts
