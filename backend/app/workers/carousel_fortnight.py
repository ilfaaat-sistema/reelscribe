"""Автономная очередь 6–19 октября: недельный gate, пилот и продолжение."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

import httpx

from app.radar.activity_job import atomic_json
from app.radar.carousel_job import run_batch
from app.workers.author_activity_daemon import Clock, retry, run_session, should_skip

WEEK_REPORT = ('https://raw.githubusercontent.com/ilfaaat-sistema/reelscribe/'
               'refs/heads/data/authors-carousels-20261013-20261019/activity-report.json')


def weekly_finished(opener=urlopen):
    """Ошибка сети/JSON и отсутствие отчёта закрывают gate; токен не передаётся."""
    try:
        with opener(WEEK_REPORT, timeout=45) as response:
            report = json.load(response)
        return (report.get('start_date') == '2026-10-13' and report.get('end_date') == '2026-10-19'
                and report.get('provider') == 'meta_graph' and report.get('paid_runs') == 0
                and report.get('collection', {}).get('status') == 'finished'
                and report.get('collection', {}).get('target') == 1600
                and report.get('collection', {}).get('checked') == 1600)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def execute(args, store, *, gate=weekly_finished, client_factory=httpx.Client, batch=run_batch,
            session=run_session, clock=None):
    clock = clock or Clock()
    # До gate не читаем Meta credentials, не создаём клиента и не загружаем очередь.
    if not gate():
        print('Ожидается завершение недельного сбора; обращения к Meta запрещены', flush=True)
        return 0
    seed = args.checkpoint_dir / 'seed'
    expected = store.build_seed(seed, args.seed_dir, os.environ.get('CAROUSEL_FORTNIGHT_AUTHORS', ''))
    retry(lambda: store.load(args.checkpoint_dir, seed), clock)
    state_path = args.checkpoint_dir / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    if should_skip(state.get('job_status')):
        print('Очередь завершена либо требует диагностики', flush=True)
        return 0

    def dispatch():
        store.github('POST', 'actions/workflows/carousels-fortnight.yml/dispatches', {'ref': 'main'})

    with client_factory(timeout=45, follow_redirects=False) as client:
        def collect(**limits):
            store.validate_checkpoint(args.checkpoint_dir, expected)
            report = batch(args.checkpoint_dir, args.output_report, token=os.environ.get('META_IG_TOKEN'),
                           account_id=os.environ.get('META_IG_USER_ID'), api_version='v23.0', client=client, **limits)
            store.validate_report(report)
            return report

        def persist(report):
            store.validate_report(report)
            return store.save(args.checkpoint_dir, args.output_report)

        if not state.get('fortnight_pilot_passed'):
            report = collect(max_seconds=120, max_requests=6)
            status = report['collection']['status']
            if status not in ('running', 'cooldown', 'finished') or report['collection']['requests'] < 1:
                retry(lambda: persist(report), clock)
                return 1
            if report['collection'].get('available', 0) < 1:
                retry(lambda: persist(report), clock)
                print('Пилот ещё не получил успешную страницу; продолжение по расписанию', flush=True)
                return 0
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(fortnight_pilot_passed=True, full_collection_enabled=True)
            atomic_json(state_path, state)
            retry(lambda: persist(report), clock)
            print('Ограниченный пилот проверен; полный сбор разрешён автоматически', flush=True)
            if status == 'finished':
                return 0
        result = session(collect=collect, persist=persist, requeue=dispatch, clock=clock,
                         max_seconds=max(1, args.max_minutes * 60 - 120),
                         log=lambda value: print(value, flush=True))
        return 0 if result in ('finished', 'continued') else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint-dir', type=Path, required=True)
    parser.add_argument('--seed-dir', type=Path, required=True)
    parser.add_argument('--output-report', type=Path, required=True)
    parser.add_argument('--max-minutes', type=int, default=330)
    args = parser.parse_args(argv)
    if not 1 <= args.max_minutes <= 330:
        parser.error('Длительность должна быть от 1 до 330 минут')
    spec = importlib.util.spec_from_file_location('carousel_fortnight_store',
            Path(__file__).resolve().parents[3] / 'scripts/carousel_fortnight_store.py')
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    try:
        return execute(args, store)
    except Exception as error:
        print('Ошибка 14-дневного сбора: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
