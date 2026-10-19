"""Быстрая сводка недельного сбора по локальной группе авторов NEYRO_SET7.

Читает один публичный ответ сайта. Имена из личного каталога остаются на компьютере.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from time import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


REPORT_URL = ('https://raw.githubusercontent.com/ilfaaat-sistema/reelscribe/refs/heads/'
              'data/authors-carousels-20261013-20261019/activity-report.json')
# В checkpoint при включении приоритета уже были завершены 219 авторов вне NEYRO_SET7.
# Пока приоритетная группа не исчерпана, новые завершения относятся только к ней.
NON_PRIORITY_BASELINE = 219


def default_authors_file() -> Path:
    project = next(parent.parent for parent in Path(__file__).resolve().parents
                   if parent.name == 'Парсер')
    return project / 'Материалы планирования/2026-10-18-авторы/authors.json'


def summary(report: dict, catalog: list[dict]) -> dict:
    names = {row['author'].lstrip('@').lower() for row in catalog
             if 'neyro_set7' in row.get('accounts', [])}
    profiles = {row['username'].lower(): row.get('carousels')
                for row in report.get('profiles', []) if isinstance(row, dict) and row.get('username')}
    checked = {name: profiles[name] for name in names
               if isinstance(profiles.get(name), int) and profiles[name] >= 0}
    collection = report.get('collection') or {}
    all_checked = collection.get('checked')
    if not isinstance(all_checked, int) or not NON_PRIORITY_BASELINE <= all_checked <= 1600:
        raise ValueError('Число обработанных авторов вне ожидаемого диапазона')
    group_checked = min(len(names), all_checked - NON_PRIORITY_BASELINE)
    if group_checked < len(checked):
        raise ValueError('Отчёт не соответствует приоритетной очереди')
    return {
        'updated_at': report.get('collected_at'),
        'status': collection.get('status'),
        'cooldown_until': collection.get('cooldown_until'),
        'neyro_checked': group_checked,
        'neyro_total': len(names),
        'neyro_pending': len(names) - group_checked,
        'neyro_carousels': sum(checked.values()),
        'all_checked': all_checked,
        'all_total': collection.get('target'),
    }


def moscow(value: str | None) -> str:
    if not value:
        return '—'
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
        ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authors-file', type=Path, default=default_authors_file())
    args = parser.parse_args()
    catalog = json.loads(args.authors_file.read_text(encoding='utf-8'))
    request = Request(f'{REPORT_URL}?v={int(time() // 60)}',
                      headers={'Accept': 'application/json'})
    with urlopen(request, timeout=10) as response:
        report = json.load(response)
    result = summary(report, catalog)
    print(f"NEYRO_SET7: {result['neyro_checked']}/{result['neyro_total']} авторов, "
          f"осталось {result['neyro_pending']}, найдено {result['neyro_carousels']} каруселей")
    print(f"Вся очередь: {result['all_checked']}/{result['all_total']}; "
          f"статус {result['status']}; обновлено {moscow(result['updated_at'])}")
    if result['status'] == 'cooldown':
        print(f"Продолжение после {moscow(result['cooldown_until'])}")


if __name__ == '__main__':
    main()
