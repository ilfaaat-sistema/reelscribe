"""Проверки очереди без сети, секретов и реальных Meta-вызовов."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.workers import carousel_fortnight as worker
from app.radar.carousel_job import run_batch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def store():
    return module('carousel_fortnight_store')


@pytest.fixture
def names():
    selection = json.loads((ROOT / 'backend/data/carousels-week-seed/selection.json').read_text())
    return [author['username'] for author in selection['authors'][:586]]


def seed(store, tmp_path, names):
    directory = tmp_path / 'seed'
    selection = store.build_seed(directory, ROOT / 'backend/data/carousels-week-seed', json.dumps(names))
    return directory, selection


def report(**changes):
    value = dict(start_date='2026-10-06', end_date='2026-10-19', timezone='Europe/Moscow',
                 provider='meta_graph', paid_runs=0, items=[],
                 collection={'target': 586, 'status': 'running', 'requests': 6, 'available': 1})
    value.update(changes)
    return value


def test_seed_preserves_followers_and_starts_empty(store, tmp_path, names):
    directory, selection = seed(store, tmp_path, names)
    assert len(selection['authors']) == 586
    source = json.loads((ROOT / 'backend/data/carousels-week-seed/selection.json').read_text())
    assert selection['authors'] == source['authors'][:586]
    assert json.loads((directory / 'state.json').read_text()) == {
        'authors': {}, 'requests': 0, 'job_status': 'running', 'full_collection_enabled': False}
    assert selection['start_date'] == '2026-10-06'
    assert all(set(author) == {'username', 'followers'} for author in selection['authors'])


@pytest.mark.parametrize('mutation', [lambda n: n[:-1], lambda n: n + [n[0]],
    lambda n: [n[0]] * 586, lambda n: ['@bad'] + n[1:],
    lambda n: ['unknown_new_author_xyz'] + n[1:], lambda n: [7] + n[1:]])
def test_seed_rejects_invalid_or_foreign_names(store, tmp_path, names, mutation):
    with pytest.raises(ValueError):
        seed(store, tmp_path, mutation(names))
    assert not (tmp_path / 'seed').exists()


def test_stores_have_distinct_globals_and_markers(store):
    weekly = module('carousel_week_store')
    assert store._store.BRANCH == 'data/authors-carousels-20261006-20261019'
    assert weekly.load.__globals__['BRANCH'] == 'data/authors-carousels-20261013-20261019'
    assert store._store.MARKER != weekly._store.MARKER


@pytest.mark.parametrize('status', ['running', 'cooldown', 'failed', None])
def test_gate_blocks_unfinished(status):
    data = dict(start_date='2026-10-13', end_date='2026-10-19', provider='meta_graph', paid_runs=0,
                collection={'status': status, 'target': 1600, 'checked': 1600})
    assert not worker.weekly_finished(lambda *a, **k: io.BytesIO(json.dumps(data).encode()))


def test_gate_finished_and_fail_closed():
    data = dict(start_date='2026-10-13', end_date='2026-10-19', provider='meta_graph', paid_runs=0,
                collection={'status': 'finished', 'target': 1600, 'checked': 1600})
    assert worker.weekly_finished(lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
    data['collection']['checked'] = 1599
    assert not worker.weekly_finished(lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
    data['collection']['checked'] = 1600
    assert not worker.weekly_finished(lambda *a, **k: io.BytesIO(b'not json'))
    def unavailable(*a, **k):
        raise OSError('network')
    assert not worker.weekly_finished(unavailable)
    data['start_date'] = '2026-10-06'
    assert not worker.weekly_finished(lambda *a, **k: io.BytesIO(json.dumps(data).encode()))


def test_gate_prevents_seed_credentials_and_meta(tmp_path):
    args = SimpleNamespace(checkpoint_dir=tmp_path, seed_dir=tmp_path, output_report=tmp_path / 'r', max_minutes=3)
    store, client, batch = Mock(), Mock(), Mock()
    assert worker.execute(args, store, gate=lambda: False, client_factory=client, batch=batch) == 0
    store.build_seed.assert_not_called()
    client.assert_not_called()
    batch.assert_not_called()


@pytest.mark.parametrize('changes', [{'paid_runs': 1}, {'start_date': '2026-10-13'},
    {'provider': 'apify'}, {'items': [{'timestamp': '2026-10-05T20:59:59+00:00', 'media_type': 'CAROUSEL_ALBUM'}]},
    {'items': [{'timestamp': '2026-10-19T21:00:00+00:00', 'media_type': 'CAROUSEL_ALBUM'}]},
    {'items': [{'timestamp': '2026-10-06T00:00:00+03:00', 'media_type': 'VIDEO'}]},
    {'items': [], 'authorization': 'anything'}])
def test_report_rejects_wrong_period_type_paid_or_secret(store, changes):
    with pytest.raises((ValueError, store._store.StoreError)):
        store.validate_report(report(**changes))


def test_report_accepts_moscow_boundaries(store):
    store.validate_report(report(items=[{'timestamp': '2026-10-05T21:00:00+00:00', 'media_type': 'CAROUSEL_ALBUM'},
                                       {'timestamp': '2026-10-19T20:59:59+00:00', 'media_type': 'CAROUSEL_ALBUM'}]))


def test_load_cannot_resume_weekly_selection(store, tmp_path, names):
    directory, selection = seed(store, tmp_path, names)
    selection['start_date'] = '2026-10-13'
    (directory / 'selection.json').write_text(json.dumps(selection))
    with pytest.raises(ValueError):
        expected = dict(selection, start_date='2026-10-06')
        store.validate_checkpoint(directory, expected)


def test_pilot_persists_then_continues_automatically(store, tmp_path, names, monkeypatch):
    monkeypatch.setenv('CAROUSEL_FORTNIGHT_AUTHORS', json.dumps(names))
    args = SimpleNamespace(checkpoint_dir=tmp_path / 'checkpoint',
                           seed_dir=ROOT / 'backend/data/carousels-week-seed',
                           output_report=tmp_path / 'report', max_minutes=3)
    original_load = store.load
    store.load = lambda directory, source: original_load(directory, source, api=lambda *a, **k: None)
    store.save = Mock()
    batch = Mock(return_value=report())
    session = Mock(return_value='continued')
    assert worker.execute(args, store, gate=lambda: True, batch=batch, session=session) == 0
    assert batch.call_args.kwargs['max_requests'] == 6
    assert batch.call_args.kwargs['max_seconds'] == 120
    assert store.save.call_count == 1
    state = json.loads((args.checkpoint_dir / 'state.json').read_text())
    assert state['fortnight_pilot_passed'] and state['full_collection_enabled']
    session.assert_called_once()


def test_inherited_collector_collects_october_six_and_deduplicates(store, tmp_path, names):
    directory, _ = seed(store, tmp_path, names)
    # Ограниченный тест: первая страница дважды содержит одну карусель.
    class Response:
        status_code = 200
        headers = {}
        def json(self):
            return {'business_discovery': {'followers_count': 10, 'media': {'data': [
                {'id': '1', 'media_type': 'CAROUSEL_ALBUM', 'timestamp': '2026-10-06T00:00:00+03:00',
                 'permalink': 'https://www.instagram.com/p/ABC/'},
                {'id': '1', 'media_type': 'CAROUSEL_ALBUM', 'timestamp': '2026-10-06T00:00:00+03:00'}]}}}
    result = run_batch(directory, tmp_path / 'r.json', token='test', account_id='123',
                       client=SimpleNamespace(get=lambda *a, **k: Response()), max_requests=2)
    assert result['totals']['carousels'] == 1
    assert result['items'][0]['timestamp'].startswith('2026-10-06')
    store.validate_report(result)


def test_cooldown_before_success_does_not_enable_full_run(store, tmp_path, names, monkeypatch):
    monkeypatch.setenv('CAROUSEL_FORTNIGHT_AUTHORS', json.dumps(names))
    args = SimpleNamespace(checkpoint_dir=tmp_path / 'checkpoint',
                           seed_dir=ROOT / 'backend/data/carousels-week-seed',
                           output_report=tmp_path / 'report', max_minutes=3)
    original_load = store.load
    store.load = lambda directory, source: original_load(directory, source, api=lambda *a, **k: None)
    store.save = Mock()
    value = report(collection={'target': 586, 'status': 'cooldown', 'requests': 1, 'available': 0})
    session = Mock()
    assert worker.execute(args, store, gate=lambda: True, batch=Mock(return_value=value), session=session) == 0
    session.assert_not_called()
    state = json.loads((args.checkpoint_dir / 'state.json').read_text())
    assert not state.get('full_collection_enabled')
    assert not state.get('fortnight_pilot_passed')
    store.save.assert_called_once()


def test_foreign_publication_is_rejected_before_github(store, tmp_path, names):
    directory, selection = seed(store, tmp_path, names)
    output = tmp_path / 'report.json'
    output.write_text(json.dumps(report(profiles=[{'username': 'someone_else'}])))
    api = Mock()
    with pytest.raises(ValueError):
        store.save(directory, output, api=api)
    api.assert_not_called()
