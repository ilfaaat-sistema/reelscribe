"""Дозаполнение просмотров/лайков/комментариев у рилсов, где reels.views пуст.

Живые рилсы (расшифровка уже готова, transcripts.status='done') иногда остаются без метрик —
скачивание аудио могло пройти через источник, который не отдал статистику, или запись легла
в базу раньше, чем в пайплайн добавили парсинг просмотров. Без views не считается главная
метрика сервиса — залётность (generated-колонка reels.er, см. §9 ТЗ и CLAUDE.md).

Скрипт дёргает одиночный платный Apify-актор apify~instagram-scraper напрямую через
run_actor_get_items — БЕЗ скачивания медиа (fetch_via_apify из apify_downloader.py качает ещё
и аудио, что здесь не нужно и тратит трафик впустую).

ВАЖНО: у этого актора videoViewCount всегда null — просмотры берём из videoPlayCount. Путаница
этих двух полей уже стоила проекту потерянных метрик, см.
docs/specs/2026-08-20_состояние-разбора.md.

Рилсы со status='no_audio' (фото/карусели) НЕ трогаем — сюда они и не попадают, потому что
выбираются только рилсы с transcripts.status='done': просмотров у фото/карусели нет по природе,
платный запрос на них был бы деньгами на ветер.

Запуск:
    python -m app.workers.backfill_metrics                 # добрать все рилсы с пустыми views
    python -m app.workers.backfill_metrics --dry-run        # только посчитать, ничего не тратить
    python -m app.workers.backfill_metrics --limit 5        # ограничить число обрабатываемых рилсов
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import httpx

from app.core.config import settings
from app.core.db import get_db
from app.pipeline.apify_client import ApifyExhaustedError, run_actor_get_items

logger = logging.getLogger(__name__)

_ACTOR = 'apify~instagram-scraper'


def _reels_missing_views(db) -> list[dict]:
    """Живые рилсы (расшифровка done) с пустыми просмотрами.

    `transcripts!inner(status)` превращает вложенный select в JOIN — .eq по transcripts.status
    отсекает родительские строки reels, а не только вложенный объект (тот же приём, что в
    app/api/reels.py). Рилсы без готовой расшифровки (в том числе no_audio) в выборку не попадают.
    """
    rows = (
        db.table('reels')
        .select('id, shortcode, url, type, author_handle, transcripts!inner(status)')
        .is_('views', 'null')
        .eq('transcripts.status', 'done')
        .execute()
        .data
    )
    return rows


def _reel_url(row: dict) -> str:
    """Прямая ссылка на пост для Apify. В базе она почти всегда есть — restore на всякий случай."""
    if row.get('url'):
        return row['url']
    kind = row.get('type') or 'reel'
    return f'https://www.instagram.com/{kind}/{row["shortcode"]}/'


async def _fetch_metrics(url: str) -> dict | None:
    """Метаданные поста одним вызовом apify~instagram-scraper, без скачивания медиа."""
    async with httpx.AsyncClient(timeout=180) as client:
        items = await run_actor_get_items(
            client, _ACTOR,
            {'directUrls': [url], 'resultsType': 'posts', 'resultsLimit': 1},
            extra_params={'memory': 256, 'timeout': 120},
        )
    if not items:
        return None
    item = items[0]
    return {
        # videoViewCount у этого актора всегда null — просмотры только из videoPlayCount.
        'views': item.get('videoPlayCount'),
        'likes': item.get('likesCount'),
        'comments': item.get('commentsCount'),
    }


async def backfill(limit: int | None = None, dry_run: bool = False) -> int:
    db = get_db()
    rows = _reels_missing_views(db)
    if limit is not None:
        rows = rows[:limit]

    logger.info('Рилсов без просмотров (расшифровка готова, не no_audio): %d', len(rows))

    if dry_run:
        for r in rows:
            logger.info('  добрать: %s (@%s)', r['shortcode'], r.get('author_handle') or '?')
        return 0

    if rows and not settings.apify_token_list:
        raise RuntimeError('APIFY_API_TOKEN не задан — добор метрик невозможен')

    updated = 0
    for i, row in enumerate(rows, 1):
        sc = row['shortcode']
        url = _reel_url(row)
        try:
            metrics = await _fetch_metrics(url)
        except ApifyExhaustedError as exc:
            logger.warning('[%d/%d] %s — Apify исчерпан, пропускаю (%s)', i, len(rows), sc, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — сбой одного рилса не должен ронять весь проход
            logger.error('[%d/%d] %s — ошибка запроса: %s', i, len(rows), sc, exc)
            continue

        if metrics is None:
            logger.warning('[%d/%d] %s — Apify вернул пустой результат', i, len(rows), sc)
            continue

        # Пишем только то, что реально пришло — не затираем уже существующие значения пустыми.
        patch = {k: v for k, v in metrics.items() if v is not None}
        if not patch:
            logger.warning(
                '[%d/%d] %s — метаданные пришли пустыми (views/likes/comments = null)', i, len(rows), sc,
            )
            continue

        db.table('reels').update(patch).eq('id', row['id']).execute()
        updated += 1
        logger.info('[%d/%d] %s → %s', i, len(rows), sc, patch)

    logger.info('Готово. Обновлено рилсов: %d из %d', updated, len(rows))
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(
        description='Дозаполнить просмотры/лайки/комментарии у рилсов с пустыми views.'
    )
    parser.add_argument('--dry-run', action='store_true', help='только показать список, ничего не тратить')
    parser.add_argument('--limit', type=int, default=None, help='ограничить число обрабатываемых рилсов')
    args = parser.parse_args()
    n = asyncio.run(backfill(limit=args.limit, dry_run=args.dry_run))
    print(f'Обновлено рилсов: {n}')


if __name__ == '__main__':
    main()
