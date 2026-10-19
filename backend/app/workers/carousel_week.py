"""Облачная очередь недельных каруселей, отдельно от завершённой активности авторов."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx

from app.radar.carousel_job import run_batch
from app.workers.author_activity_daemon import Clock, retry, run_session, should_skip, transient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint-dir', type=Path, required=True)
    parser.add_argument('--seed-dir', type=Path, required=True)
    parser.add_argument('--output-report', type=Path, required=True)
    parser.add_argument('--max-minutes', type=int, default=330)
    parser.add_argument('--pilot', choices=('true', 'false'), default='false')
    parser.add_argument('--start-full', choices=('true', 'false'), default='false')
    args = parser.parse_args(argv)
    if not 1 <= args.max_minutes <= 330:
        parser.error('Длительность должна быть от 1 до 330 минут')
    spec = importlib.util.spec_from_file_location('carousel_week_store', Path(__file__).resolve().parents[3] / 'scripts/carousel_week_store.py')
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    clock = Clock()
    pilot = args.pilot == 'true'

    def dispatch():
        store.github('POST', 'actions/workflows/carousels-week.yml/dispatches', {'ref': 'main'})
    try:
        try:
            retry(lambda: store.load(args.checkpoint_dir, args.seed_dir), clock)
        except Exception as error:
            if not transient(error) or pilot:
                raise
            retry(dispatch, clock)
            print('Очередь временно недоступна; передана следующему запуску', flush=True)
            return 0
        state_path = args.checkpoint_dir / 'state.json'
        state = json.loads(state_path.read_text())
        if should_skip(state.get('job_status')):
            print('Очередь завершена либо требует диагностики', flush=True)
            return 0
        if args.start_full == 'true':
            state['full_collection_enabled'] = True
            state_path.write_text(json.dumps(state, ensure_ascii=False))
        if not pilot and not state.get('full_collection_enabled'):
            print('Ожидается приёмка пилота; массовый сбор ещё не включён', flush=True)
            return 0
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            def collect(**limits):
                first = os.environ.get('CAROUSEL_FIRST_AUTHORS', '')
                remaining = os.environ.get('CAROUSEL_PRIORITY_AUTHORS', '')
                return run_batch(args.checkpoint_dir, args.output_report, token=os.environ.get('META_IG_TOKEN'),
                                 account_id=os.environ.get('META_IG_USER_ID'), api_version='v23.0', client=client,
                                 priority_authors=','.join(part for part in (first, remaining) if part), **limits)
            def persist(_report):
                retry(lambda: store.save(args.checkpoint_dir, args.output_report), clock)
            if pilot:
                report = collect(max_seconds=120, max_requests=6)
                persist(report)
                print(f"Пилот сохранён: {report['collection']['checked']} авторов, {report['totals']['carousels']} каруселей", flush=True)
                return 0 if report['collection']['status'] in ('running', 'cooldown', 'finished') else 1
            result = run_session(collect=collect, persist=lambda _: store.save(args.checkpoint_dir, args.output_report),
                                 requeue=dispatch, max_seconds=args.max_minutes * 60,
                                 log=lambda value: print(value, flush=True))
            return 0 if result in ('finished', 'continued') else 1
    except Exception as error:
        print('Ошибка недельного сбора: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
