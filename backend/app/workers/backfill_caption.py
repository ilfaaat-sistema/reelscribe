"""Дозаполнение перевода текста поста (caption_ru) у рилсов, где его ещё нет.

Перевод подписи — штатная стадия пайплайна (app/workers/run.py, рядом с переводом
расшифровки), но часть уже сохранённых рилсов остаётся без caption_ru: подписи, сохранённые
до появления этой фичи (ТЗ 07), и рилсы, у которых на момент импорта перевод не был включён.
Скрипт находит такие записи в reels и прогоняет caption через translate_to_ru_detailed отдельным
проходом — по тому же правилу needs_translation, которым судит и пайплайн
(app/pipeline/translate.py), чтобы решение «нужно ли переводить» не расходилось. Заодно
сохраняет caption_lang (язык оригинала, из detected_source_lang DeepL) — миграция 0007.

Отдельный режим --lang-only — для 439 подписей, у которых caption_ru уже есть, а caption_lang
ещё пуст (перевод сделан ДО того, как в БД появилась колонка caption_lang). Чтобы не платить
квотой DeepL за повторный перевод всего текста ради одного поля языка, отправляет только
первые ~200 символов подписи и берёт из ответа detected_source_lang, сам перевод отбрасывает
(см. translate.detect_caption_lang).

Запуск:
    python -m app.workers.backfill_caption                     # все подходящие подписи (перевод + язык)
    python -m app.workers.backfill_caption --dry-run            # только посчитать, ничего не переводить
    python -m app.workers.backfill_caption --limit 20           # ограничить число переводов за прогон
    python -m app.workers.backfill_caption --session <UUID>     # только рилсы указанной сессии
    python -m app.workers.backfill_caption --lang-only           # только доопределить caption_lang
    python -m app.workers.backfill_caption --lang-only --dry-run # посчитать кандидатов, без запросов
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.db import get_db
from app.pipeline.translate import (
    TranslationFailedError,
    TranslationQuotaExceededError,
    needs_translation,
    translate_to_ru_detailed,
)

logger = logging.getLogger(__name__)

_PROGRESS_EVERY = 10
_PAUSE_SEC = 0.7  # пауза между записями, чтобы бесплатный Google Translate не резал по частоте
_PAGE = 1000  # размер страницы PostgREST — его же дефолтный максимум (см. app/api/export_.py::_fetch)
_LANG_ONLY_SAMPLE_LEN = 200  # символов подписи, которых DeepL достаточно, чтобы определить язык


def _reel_ids_for_session(db, session_id: str) -> list[str]:
    jobs = db.table('jobs').select('reel_id').eq('session_id', session_id).execute()
    return [j['reel_id'] for j in jobs.data]


def _candidates(db, session_id: str | None) -> list[dict]:
    """Рилсы с непустым caption и пустым caption_ru, подпадающие под правило needs_translation.

    Идемпотентность обеспечивает сам фильтр `caption_ru is null`: уже переведённые записи
    (или сознательно пропущенные при переводе как русские — тем не менее с выставленным
    caption_ru) в выборку повторно не попадают.
    """
    query = (
        db.table('reels')
        .select('id, shortcode, caption')
        .not_.is_('caption', 'null')
        .neq('caption', '')
        .is_('caption_ru', 'null')
        .order('id')  # без явного порядка постраничный range() не гарантирует полное покрытие
    )
    if session_id:
        ids = _reel_ids_for_session(db, session_id)
        if not ids:
            logger.info('В сессии %s нет рилсов', session_id)
            return []
        query = query.in_('id', ids)

    # Без range() PostgREST молча отдаёт только первую страницу (~1000 строк) — на 2687
    # подписях это давало заниженный в 2.7 раза dry-run (259 вместо ожидаемых ~700),
    # найдено на приёмке 23.09.2026. Пагинируем так же, как app/api/export_.py::_fetch.
    rows: list[dict] = []
    page = 0
    while True:
        chunk = query.range(page * _PAGE, (page + 1) * _PAGE - 1).execute().data
        rows.extend(chunk)
        if len(chunk) < _PAGE:
            break
        page += 1

    return [r for r in rows if needs_translation(r.get('caption'))]


def _lang_only_candidates(db, session_id: str | None) -> list[dict]:
    """Рилсы, где caption_ru уже переведён, а caption_lang ещё пуст (см. --lang-only).

    Фильтр `caption_lang is null` даёт идемпотентность так же, как `caption_ru is null` — у
    основного режима: повторный прогон не трогает уже размеченные записи.
    """
    query = (
        db.table('reels')
        .select('id, shortcode, caption')
        .not_.is_('caption', 'null')
        .neq('caption', '')
        .not_.is_('caption_ru', 'null')
        .neq('caption_ru', '')
        .is_('caption_lang', 'null')
        .order('id')
    )
    if session_id:
        ids = _reel_ids_for_session(db, session_id)
        if not ids:
            logger.info('В сессии %s нет рилсов', session_id)
            return []
        query = query.in_('id', ids)

    rows: list[dict] = []
    page = 0
    while True:
        chunk = query.range(page * _PAGE, (page + 1) * _PAGE - 1).execute().data
        rows.extend(chunk)
        if len(chunk) < _PAGE:
            break
        page += 1

    return rows


async def backfill(
    session_id: str | None = None, limit: int | None = None, dry_run: bool = False,
) -> tuple[int, int]:
    db = get_db()
    rows = _candidates(db, session_id)
    if limit is not None:
        rows = rows[:limit]

    total = len(rows)
    logger.info('Подписей без перевода, подпадающих под правило: %d', total)

    if dry_run:
        for r in rows:
            logger.info('  переведу: %s', r['shortcode'])
        return 0, 0

    translated = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        try:
            caption_ru, caption_lang = await asyncio.to_thread(translate_to_ru_detailed, row['caption'])
            update = {'caption_ru': caption_ru}
            if caption_lang:
                update['caption_lang'] = caption_lang
            db.table('reels').update(update).eq('id', row['id']).execute()
            translated += 1
        except TranslationQuotaExceededError as exc:
            # Квота/ключ DeepL не восстановятся за секунды — не перебираем впустую остаток.
            logger.warning('Квота DeepL исчерпана (%s) — останавливаю прогон на записи %d/%d', exc, i, total)
            break
        except TranslationFailedError as exc:
            failed += 1
            logger.warning(
                'Рилс %s (%s): перевод подписи не прошёл проверку (%s) — caption_ru не пишу',
                row['id'], row.get('shortcode'), exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 — сбой одной записи не должен ронять весь проход
            failed += 1
            logger.warning(
                'Рилс %s (%s): перевод подписи не удался — %s', row['id'], row.get('shortcode'), exc,
            )

        if i % _PROGRESS_EVERY == 0 or i == total:
            logger.info('Прогресс: %d/%d (переведено %d, ошибок %d)', i, total, translated, failed)

        if i < total:
            await asyncio.sleep(_PAUSE_SEC)

    logger.info('Готово. Переведено: %d, не удалось: %d', translated, failed)
    return translated, failed


async def backfill_lang_only(
    session_id: str | None = None, limit: int | None = None, dry_run: bool = False,
) -> tuple[int, int]:
    """Доопределяет caption_lang у уже переведённых подписей, не трогая caption_ru.

    Требует DEEPL_API_KEY — без него определять язык нечем (см. translate.detect_caption_lang).
    В dry-run сети не касается вовсе, ключ не проверяется, чтобы --dry-run работал и без него.
    """
    from app.core.config import settings
    from app.pipeline.translate import detect_caption_lang

    db = get_db()
    rows = _lang_only_candidates(db, session_id)
    if limit is not None:
        rows = rows[:limit]

    total = len(rows)
    logger.info('Подписей с переводом, но без caption_lang: %d', total)

    if dry_run:
        for r in rows:
            logger.info('  определю язык: %s', r['shortcode'])
        return 0, 0

    if not settings.deepl_api_key:
        raise RuntimeError('--lang-only требует DEEPL_API_KEY — без DeepL язык не определить')

    done = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        try:
            lang = await asyncio.to_thread(detect_caption_lang, row['caption'], _LANG_ONLY_SAMPLE_LEN)
            if lang:
                db.table('reels').update({'caption_lang': lang}).eq('id', row['id']).execute()
                done += 1
            else:
                logger.warning('Рилс %s (%s): DeepL не вернул язык', row['id'], row.get('shortcode'))
        except TranslationQuotaExceededError as exc:
            logger.warning('Квота DeepL исчерпана (%s) — останавливаю прогон на записи %d/%d', exc, i, total)
            break
        except Exception as exc:  # noqa: BLE001 — сбой одной записи не должен ронять весь проход
            failed += 1
            logger.warning(
                'Рилс %s (%s): определение языка не удалось — %s', row['id'], row.get('shortcode'), exc,
            )

        if i % _PROGRESS_EVERY == 0 or i == total:
            logger.info('Прогресс: %d/%d (определено %d, ошибок %d)', i, total, done, failed)

        if i < total:
            await asyncio.sleep(_PAUSE_SEC)

    logger.info('Готово. Определено языков: %d, не удалось: %d', done, failed)
    return done, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Дозаполнить перевод текста поста (caption_ru) и его язык (caption_lang).')
    parser.add_argument('--session', help='только рилсы указанной сессии (UUID)')
    parser.add_argument('--dry-run', action='store_true', help='только посчитать, ничего не переводить')
    parser.add_argument('--limit', type=int, default=None, help='ограничить число обработанных записей за прогон')
    parser.add_argument(
        '--lang-only', action='store_true',
        help='не переводить заново, только доопределить caption_lang у уже переведённых подписей',
    )
    args = parser.parse_args()
    if args.lang_only:
        done, failed = asyncio.run(backfill_lang_only(args.session, args.limit, args.dry_run))
        print(f'Определено языков: {done}, не удалось: {failed}')
    else:
        translated, failed = asyncio.run(backfill(args.session, args.limit, args.dry_run))
        print(f'Переведено: {translated}, не удалось: {failed}')


if __name__ == '__main__':
    main()
