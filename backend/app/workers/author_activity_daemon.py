"""Продолжать серверные порции и передавать очередь следующему раннеру без cron."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

import httpx

from app.radar.activity_job import run_batch


class Clock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


def transient(error):
    code = getattr(error, 'status_code', None)
    return isinstance(error, (URLError, TimeoutError, ConnectionError)) or code == 429 or (code is not None and code >= 500)


def retry(operation, clock):
    for attempt in range(3):
        try:
            return operation()
        except Exception as error:
            if not transient(error) or attempt == 2:
                raise
            clock.sleep(5 * (attempt + 1))


def should_skip(status, retry_blocked=False):
    return status == 'finished' or (status in ('authentication_required', 'permissions_or_request_error', 'failed')
                                    and not retry_blocked)


def run_session(*, collect, persist, requeue, clock=None, max_seconds=19800, log=print):
    """Инъецируемые операции позволяют проверить ожидание/передачу без сети и сна."""
    clock = clock or Clock()
    deadline = clock.monotonic() + max_seconds
    margin = min(120, max_seconds / 10)
    last = None

    def continue_elsewhere():
        retry(requeue, clock)
        log('Продолжение очереди отправлено следующему серверному запуску')
        return 'continued'

    while True:
        remaining = deadline - clock.monotonic() - margin
        if remaining < 1:
            return continue_elsewhere()
        report = collect(max_seconds=min(480, max(1, int(remaining))), max_requests=20)
        collection = report['collection']
        status = collection['status']
        if status not in ('running', 'cooldown', 'finished', 'authentication_required', 'permissions_or_request_error', 'failed'):
            raise ValueError('Неизвестный статус серверной очереди')
        # После каждой порции checkpoint и отчёт должны быть сохранены вместе.
        for attempt in range(3):
            try:
                persist(report)
                break
            except Exception as error:
                if not transient(error):
                    raise
                if attempt == 2:
                    log('Публикация временно недоступна; следующая попытка восстановит последнюю серверную точку')
                    return continue_elsewhere()
                clock.sleep(5 * (attempt + 1))
        log(f"{status}: проверено {collection['checked']}/{collection['target']}; "
            f"каруселей {report['totals']['carousels']}; попыток {collection['requests']}")
        if status == 'finished':
            return 'finished'
        if status in ('authentication_required', 'permissions_or_request_error', 'failed'):
            return status
        progress = (collection['checked'], collection['requests'])
        if status == 'running' and progress == last:
            # Защита от ошибочного бесконечного цикла без единой новой попытки.
            raise RuntimeError('Очередь не продвигается между порциями')
        last = progress
        if status == 'cooldown':
            until = datetime.fromisoformat(collection['cooldown_until'].replace('Z', '+00:00'))
            if until.tzinfo is None:
                raise ValueError('Пауза Meta без часового пояса')
            log('Пауза Meta сохранена; серверный раннер продолжит без нового события cron')
            while True:
                wait = (until - clock.now()).total_seconds()
                remaining = deadline - clock.monotonic() - margin
                if remaining < 1:
                    return continue_elsewhere()
                if wait <= 0:
                    break
                clock.sleep(min(wait, remaining, 45))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint-dir', type=Path, required=True)
    parser.add_argument('--seed-dir', type=Path, required=True)
    parser.add_argument('--output-report', type=Path, required=True)
    parser.add_argument('--max-minutes', type=int, default=330)
    parser.add_argument('--retry-blocked', choices=('true', 'false'), default='false')
    args = parser.parse_args(argv)
    if not 1 <= args.max_minutes <= 330:
        parser.error('Длительность серверной сессии: 1–330 минут')
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('activity_store', root / 'scripts/author_activity_store.py')
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    try:
        clock = Clock()
        def dispatch():
            store.github('POST', 'actions/workflows/authors-activity.yml/dispatches', {'ref': 'main'})
        try:
            retry(lambda: store.load(args.checkpoint_dir, args.seed_dir), clock)
        except Exception as error:
            if not transient(error):
                raise
            retry(dispatch, clock)
            print('Восстановление временно недоступно; отправлен следующий серверный запуск', flush=True)
            return 0
        state = json.loads((args.checkpoint_dir / 'state.json').read_text())
        status = state.get('job_status')
        if should_skip(status, args.retry_blocked == 'true'):
            print('Очередь завершена или требует явного восстановления: ' + status, flush=True)
            return 0
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            def collect(**limits):
                return run_batch(args.checkpoint_dir, args.output_report,
                                 token=os.environ.get('META_IG_TOKEN'), account_id=os.environ.get('META_IG_USER_ID'),
                                 api_version=os.environ.get('META_IG_API_VERSION', 'v23.0'), client=client, **limits)

            def persist(report):
                store.save(args.checkpoint_dir, args.output_report)

            def requeue():
                # Это событие разрешено штатному GITHUB_TOKEN и не зависит от cron.
                dispatch()

            result = run_session(collect=collect, persist=persist, requeue=requeue,
                                 max_seconds=args.max_minutes * 60, log=lambda value: print(value, flush=True))
        return 0 if result in ('finished', 'continued') else 1
    except Exception as error:
        # Исключения HTTP могут содержать URL; значения секретов и сырые ответы не выводим.
        print('Ошибка серверного продолжения: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
