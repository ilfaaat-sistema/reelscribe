"""Дозаполнение подписчиков у рилсов, где author_followers пуст.

Раньше подписчиков давал только StarAPI, и при исчерпании квоты они оставались NULL —
из-за чего не считались метрики er/lpf/cpf. Скрипт тянет их через resolve_followers
(Apify profile-актор → instaloader-сессия), один вызов на автора, и проставляет в reels.
er/lpf/cpf — generated-колонки в Postgres, пересчитаются автоматически при обновлении.

Запуск:
    python -m app.workers.backfill_followers                 # все рилсы с пустыми подписчиками
    python -m app.workers.backfill_followers --session <UUID> # только рилсы указанной сессии
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.db import get_db
from app.pipeline.followers import resolve_followers

logger = logging.getLogger(__name__)


def _reel_ids_for_session(db, session_id: str) -> list[str]:
    jobs = db.table('jobs').select('reel_id').eq('session_id', session_id).execute()
    return [j['reel_id'] for j in jobs.data]


async def backfill(session_id: str | None = None) -> int:
    db = get_db()
    query = (
        db.table('reels')
        .select('id, author_handle')
        .is_('author_followers', 'null')
        .not_.is_('author_handle', 'null')
    )
    if session_id:
        ids = _reel_ids_for_session(db, session_id)
        if not ids:
            logger.info('В сессии %s нет рилсов', session_id)
            return 0
        query = query.in_('id', ids)

    rows = query.execute().data
    # уникальные авторы → один вызов instaloader на автора (кэш внутри хелпера тоже сработает)
    authors = sorted({r['author_handle'] for r in rows if r.get('author_handle')})
    logger.info('Пустых подписчиков: %d рилсов, %d авторов', len(rows), len(authors))

    updated = 0
    for i, author in enumerate(authors, 1):
        followers = await resolve_followers(author)
        if followers is None:
            logger.warning('[%d/%d] @%s — подписчиков не получить, пропускаю', i, len(authors), author)
            continue
        res = (
            db.table('reels')
            .update({'author_followers': followers})
            .eq('author_handle', author)
            .is_('author_followers', 'null')
            .execute()
        )
        n = len(res.data or [])
        updated += n
        logger.info('[%d/%d] @%s → %d подписчиков, обновлено рилсов: %d', i, len(authors), author, followers, n)

    logger.info('Готово. Обновлено рилсов: %d', updated)
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Дозаполнить подписчиков через instaloader.')
    parser.add_argument('--session', help='только рилсы указанной сессии (UUID)')
    args = parser.parse_args()
    n = asyncio.run(backfill(args.session))
    print(f'Обновлено рилсов: {n}')


if __name__ == '__main__':
    main()
