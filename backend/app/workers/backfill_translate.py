"""Дозаполнение перевода у расшифровок, где text_ru пуст.

Перевод — штатная стадия пайплайна (app/pipeline/translate.py), но часть готовых
расшифровок на иностранных языках могла остаться без перевода (сбой в моменте, стадия
перевода добавлена позже и т.п.). Скрипт находит такие записи в transcripts и прогоняет
их через translate_to_ru отдельным проходом, не трогая сам пайплайн.

Запуск:
    python -m app.workers.backfill_translate                 # все расшифровки с пустым text_ru
    python -m app.workers.backfill_translate --session <UUID> # только рилсы указанной сессии
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.db import get_db
from app.pipeline.translate import translate_to_ru

logger = logging.getLogger(__name__)

_PROGRESS_EVERY = 10
_PAUSE_SEC = 0.7  # пауза между записями, чтобы бесплатный Google Translate не резал по частоте


def _reel_ids_for_session(db, session_id: str) -> list[str]:
    jobs = db.table('jobs').select('reel_id').eq('session_id', session_id).execute()
    return [j['reel_id'] for j in jobs.data]


async def backfill(session_id: str | None = None) -> tuple[int, int]:
    db = get_db()
    query = (
        db.table('transcripts')
        .select('id, reel_id, text, language')
        .eq('status', 'done')
        .not_.is_('text', 'null')
        .neq('text', '')
        .is_('text_ru', 'null')
        .not_.is_('language', 'null')
        .neq('language', 'ru')
    )
    if session_id:
        ids = _reel_ids_for_session(db, session_id)
        if not ids:
            logger.info('В сессии %s нет рилсов', session_id)
            return 0, 0
        query = query.in_('reel_id', ids)

    rows = query.execute().data
    total = len(rows)
    logger.info('Расшифровок без перевода: %d', total)

    translated = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        try:
            text_ru = await asyncio.to_thread(translate_to_ru, row['text'])
            db.table('transcripts').update({'text_ru': text_ru}).eq('id', row['id']).execute()
            translated += 1
        except Exception as exc:  # noqa: BLE001 — сбой одной записи не должен ронять весь проход
            failed += 1
            logger.warning('Расшифровка %s (%s): перевод не удался — %s', row['id'], row.get('language'), exc)

        if i % _PROGRESS_EVERY == 0 or i == total:
            logger.info('Прогресс: %d/%d (переведено %d, ошибок %d)', i, total, translated, failed)

        if i < total:
            await asyncio.sleep(_PAUSE_SEC)

    logger.info('Готово. Переведено: %d, не удалось: %d', translated, failed)
    return translated, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Дозаполнить перевод расшифровок (text_ru).')
    parser.add_argument('--session', help='только рилсы указанной сессии (UUID)')
    args = parser.parse_args()
    translated, failed = asyncio.run(backfill(args.session))
    print(f'Переведено: {translated}, не удалось: {failed}')


if __name__ == '__main__':
    main()
