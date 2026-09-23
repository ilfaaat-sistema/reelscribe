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

    Тонкая обёртка над translate_to_ru_detailed для мест, которым язык оригинала не нужен
    (старая сигнатура, не трогаем — используется в нескольких местах пайплайна).
    """
    return translate_to_ru_detailed(text)[0]


def translate_to_ru_detailed(text: str) -> tuple[str, str | None]:
    """Как translate_to_ru, но вместе с языком оригинала.

    Язык узнаём у самого DeepL: он и так определяет исходный язык, чтобы перевести
    (result.detected_source_lang, двухбуквенный код вроде 'EN', 'TR', 'HI' — приводим к
    нижнему регистру). Отдельный определитель языка не нужен и не заводим. Если перевод
    шёл через запасной Google (нет DEEPL_API_KEY, либо DeepL временно недоступен и часть
    кусков ушла через Google-фолбэк внутри _deepl) — для таких кусков языка нет, возвращаем
    None; это нормально и не ошибка (см. вызывающий код).
    """
    if not text or not text.strip():
        return text, None
    from app.core.config import settings
    chunks = _split(text.strip(), _MAX_CHUNK)
    if settings.deepl_api_key:
        return _deepl(chunks, settings.deepl_api_key)
    return _google(chunks), None


def detect_caption_lang(text: str, sample_len: int = 200) -> str | None:
    """Язык оригинала текста через DeepL — без записи перевода.

    Только для бэкфилла caption_lang у подписей, у которых caption_ru уже переведён (см.
    backfill_caption.py --lang-only): переводить текст заново ради одного поля языка — трата
    квоты, а определять его отдельным детектором — лишняя зависимость. DeepL всё равно
    определяет исходный язык, чтобы перевести, поэтому шлём в него только первые sample_len
    символов текста, а сам перевод из ответа отбрасываем.

    Бросает TranslationQuotaExceededError на исчерпании квоты/невалидном ключе — как и
    остальные функции модуля, с тем же предохранителем на процесс. Работает только с DeepL:
    у Google (deep_translator) языка в ответе нет — вызывающий код должен сам проверить
    settings.deepl_api_key до вызова.
    """
    global _deepl_disabled_reason
    import deepl
    from app.core.config import settings

    if not text or not text.strip():
        return None
    if not settings.deepl_api_key:
        raise RuntimeError('detect_caption_lang требует DEEPL_API_KEY (иначе языка не узнать)')
    if _deepl_disabled_reason is not None:
        raise TranslationQuotaExceededError(_deepl_disabled_reason)

    sample = text.strip()[:sample_len]
    translator = deepl.Translator(settings.deepl_api_key)
    try:
        result = translator.translate_text(sample, target_lang='RU')
    except (deepl.QuotaExceededException, deepl.AuthorizationException) as exc:
        _deepl_disabled_reason = f'{type(exc).__name__}: {exc}'
        logger.error('DeepL: %s — дальше в этом прогоне DeepL не используется', _deepl_disabled_reason)
        raise TranslationQuotaExceededError(_deepl_disabled_reason) from exc

    if isinstance(result, list):
        lang = result[0].detected_source_lang if result else None
    else:
        lang = result.detected_source_lang
    return lang.lower() if lang else None


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


def _deepl(chunks: list[str], api_key: str) -> tuple[str, str | None]:
    """Официальный пакет deepl (не deep_translator — см. ТЗ: только у него отличимы квота/сеть).

    При исчерпании квоты или неверном ключе — TranslationQuotaExceededError и предохранитель
    на остаток процесса (без фолбэка на Google, чтобы переход на DeepL не терялся в первый же
    день). При прочих ошибках DeepL — предупреждение в лог и фолбэк на Google для этого куска
    (для такого куска язык не узнаём — см. translate_to_ru_detailed).

    Возвращает (перевод, язык): язык — из detected_source_lang первого куска, где DeepL его
    определил; при нескольких кусках одного текста язык один и тот же, дальше не уточняем.
    """
    global _deepl_disabled_reason
    import deepl

    if _deepl_disabled_reason is not None:
        raise TranslationQuotaExceededError(_deepl_disabled_reason)

    translator = deepl.Translator(api_key)
    parts: list[str] = []
    detected_lang: str | None = None
    for chunk in chunks:
        try:
            result = translator.translate_text(chunk, target_lang='RU')
            if isinstance(result, list):
                translated = ' '.join(r.text for r in result)
                chunk_lang = result[0].detected_source_lang if result else None
            else:
                translated = result.text
                chunk_lang = result.detected_source_lang
        except (deepl.QuotaExceededException, deepl.AuthorizationException) as exc:
            _deepl_disabled_reason = f'{type(exc).__name__}: {exc}'
            logger.error('DeepL: %s — дальше в этом прогоне DeepL не используется', _deepl_disabled_reason)
            raise TranslationQuotaExceededError(_deepl_disabled_reason) from exc
        except deepl.DeepLException as exc:
            logger.warning('DeepL chunk failed (%s), fallback to Google', exc)
            parts.append(_google([chunk]))  # _google уже валидирует и бросает при браке
            continue
        parts.append(_validate_chunk(chunk, translated))
        if detected_lang is None and chunk_lang:
            detected_lang = chunk_lang.lower()
    return ' '.join(parts), detected_lang


def _split(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts
