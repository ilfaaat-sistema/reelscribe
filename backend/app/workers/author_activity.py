"""CLI для ограниченного серверного batch Instagram Business Discovery."""
from __future__ import annotations

import argparse
import os

import httpx

from app.radar.activity_job import run_batch


def main(argv=None):
    parser = argparse.ArgumentParser(description='Возобновляемый сбор активности авторов через Meta Graph')
    parser.add_argument('--checkpoint-dir', required=True)
    parser.add_argument('--output-report', required=True)
    parser.add_argument('--max-seconds', type=int, default=480)
    parser.add_argument('--max-requests', type=int, default=20)
    args = parser.parse_args(argv)
    if not 0 < args.max_seconds <= 480 or not 0 < args.max_requests <= 20:
        parser.error('Лимиты запуска: 1–480 секунд и 1–20 запросов')
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        report = run_batch(args.checkpoint_dir, args.output_report,
                           token=os.environ.get('META_IG_TOKEN'),
                           account_id=os.environ.get('META_IG_USER_ID'),
                           api_version=os.environ.get('META_IG_API_VERSION', 'v23.0'),
                           max_seconds=args.max_seconds, max_requests=args.max_requests,
                           client=client)
    collection = report['collection']
    print(f"{collection['status']}: {collection['checked']}/{collection['target']}; "
          f"запросов всего {collection['requests']}")
    return 1 if collection['status'] in ('failed', 'authentication_required',
                                         'permissions_or_request_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
