"""Изолированное хранилище фиксированного периода 6–19 октября."""
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location('fortnight_checkpoint_store', Path(__file__).with_name('author_activity_store.py'))
_store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_store)
_store.BRANCH = 'data/authors-carousels-20261006-20261019'
_store.MARKER = {'kind': 'reelscribe-carousels-fortnight', 'version': 1}
github = _store.github
TARGET = 586
PERIOD = {'start_date': '2026-10-06', 'end_date': '2026-10-19', 'timezone': 'Europe/Moscow'}


def build_seed(seed_dir, source_dir, private_names):
    """Из закрытого JSON-массива имён переносим только публичные поля seed."""
    names = json.loads(private_names)
    if (not isinstance(names, list) or len(names) != TARGET or
            any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9._]{1,30}', name) for name in names) or
            len(set(names)) != TARGET):
        raise ValueError('Нужны ровно 586 уникальных допустимых имён')
    source = json.loads((source_dir / 'selection.json').read_text(encoding='utf-8'))
    authors = source['authors']
    lookup = {author['username']: author for author in authors}
    if len(authors) != 1600 or len(lookup) != 1600 or not set(names) <= set(lookup):
        raise ValueError('Выборка должна входить в исходные 1600 авторов')
    selection = dict(PERIOD, authors=[{'username': name, 'followers': lookup[name].get('followers')}
                                    for name in names], total=TARGET, provider='meta_graph', paid_runs=0)
    state = {'authors': {}, 'requests': 0, 'job_status': 'running', 'full_collection_enabled': False}
    _store.validate_data(selection)
    seed_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (('selection', selection), ('state', state)):
        (seed_dir / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return selection


def validate_checkpoint(directory, expected):
    selection = json.loads((directory / 'selection.json').read_text(encoding='utf-8'))
    state = json.loads((directory / 'state.json').read_text(encoding='utf-8'))
    if selection != expected or not set(state.get('authors', {})) <= {a['username'] for a in expected['authors']}:
        raise ValueError('Контрольная точка принадлежит другой выборке')
    _store.validate_data(selection)
    _store.validate_data(state)


def validate_report(report):
    if (any(report.get(key) != value for key, value in PERIOD.items()) or
            report.get('provider') != 'meta_graph' or report.get('paid_runs') != 0 or
            report.get('collection', {}).get('target') != TARGET):
        raise ValueError('Отчёт другого периода или провайдера')
    first = datetime(2026, 10, 6, tzinfo=ZoneInfo('Europe/Moscow'))
    end = datetime(2026, 10, 20, tzinfo=ZoneInfo('Europe/Moscow'))
    for item in report.get('items', []):
        stamp = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
        if item.get('media_type') != 'CAROUSEL_ALBUM' or stamp.tzinfo is None or not first <= stamp < end:
            raise ValueError('Карточка неверного типа или даты')
    _store.validate_data(report)


def load(directory, seed, api=None):
    expected = json.loads((seed / 'selection.json').read_text(encoding='utf-8'))
    result = _store.load(directory, seed, api=api or github)
    validate_checkpoint(directory, expected)
    return result


def save(directory, report_path, api=None):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    validate_report(report)
    selection = json.loads((directory / 'selection.json').read_text(encoding='utf-8'))
    names = [author['username'] for author in selection['authors']]
    if (any(selection.get(key) != value for key, value in PERIOD.items()) or
            len(names) != TARGET or len(set(names)) != TARGET or
            [profile['username'] for profile in report.get('profiles', [])] != names or
            any(item.get('username') not in names for item in report.get('items', []))):
        raise ValueError('Отчёт не соответствует изолированной выборке')
    validate_checkpoint(directory, selection)
    return _store.save(directory, report_path, api=api or github)
