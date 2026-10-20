"""Облачная очередь живых избранных каруселей без платных провайдеров."""
import os
import sys

import httpx

from app.radar.live_carousel_job import run
from app.radar.live_favorites_store import LiveFavoritesStore, WorkerStore

HISTORICAL_WORKFLOWS = ('carousels-week.yml', 'carousels-fortnight.yml')


def historical_collectors_active(client, token):
    """При ошибке проверки закрываем gate: не конкурируем за общую квоту Meta."""
    if not token:
        raise ValueError('Нужен доступ для проверки активных очередей GitHub')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    for workflow in HISTORICAL_WORKFLOWS:
        for status in ('in_progress', 'queued'):
            response = client.get(
                f'https://api.github.com/repos/ilfaaat-sistema/reelscribe/actions/workflows/{workflow}/runs',
                headers=headers, params={'status': status, 'per_page': 1}, timeout=30)
            response.raise_for_status()
            if response.json().get('total_count', 0) > 0:
                return True
    return False


def main():
    try:
        with httpx.Client(follow_redirects=False) as client:
            store = WorkerStore(LiveFavoritesStore(client, os.environ.get('GH_TOKEN')))
            if historical_collectors_active(client, os.environ.get('GH_TOKEN')):
                print('Исторический сбор Meta активен; очередь избранного ждёт следующего запуска')
                return 0
            result = run(store, client, token=os.environ.get('META_IG_TOKEN'),
                         account_id=os.environ.get('META_IG_USER_ID'),
                         api_version=os.environ.get('META_IG_API_VERSION', 'v23.0'))
            print(f"Сессия: {result['status']}; запросов Meta: {result['requests']}")
            return 0
    except Exception as error:  # noqa: BLE001 — HTTP исключения могут содержать секреты.
        # HTTP исключения могут содержать секретные параметры; печатается только класс.
        print('Ошибка очереди избранного: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
