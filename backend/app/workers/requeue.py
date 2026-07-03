"""Массовый ретрай застрявших рилсов.

Сбрасывает незавершённые джобы (failed + queued) обратно в очередь, чтобы воркер
подхватил их заново (например, после добавления RapidAPI-ключей).

Запуск:
    python -m app.workers.requeue --all
    python -m app.workers.requeue --session <UUID>
"""
from __future__ import annotations

import argparse
import logging

from app.services.jobs import requeue_jobs


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Повторить незавершённые рилсы.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all', action='store_true', help='все сессии')
    group.add_argument('--session', help='только указанная сессия (UUID)')
    args = parser.parse_args()

    n = requeue_jobs(None if args.all else args.session)
    print(f'Повторно поставлено в очередь джобов: {n}')


if __name__ == '__main__':
    main()
