"""Проверки облачной контрольной точки без GitHub и учётных данных."""
import base64
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('activity_store', Path(__file__).resolve().parents[2] / 'scripts/author_activity_store.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class GitHub:
    def __init__(self):
        self.reference = None
        self.blobs, self.trees, self.commits = {}, {}, {}

    def __call__(self, method, path, payload=None, missing=False):
        if path.startswith('git/ref/heads/'):
            return self.reference
        if method == 'GET' and path.startswith('git/blobs/'):
            return {'size': 100, 'encoding': 'base64', 'content': self.blobs[path.split('/')[-1]]}
        if method == 'GET' and path.startswith('git/commits/'):
            return self.commits[path.split('/')[-1]]
        if method == 'GET' and path.startswith('git/trees/'):
            return {'tree': self.trees[path.split('/')[-1].split('?')[0]]}
        if method == 'POST' and path == 'git/blobs':
            sha = 'blob' + str(len(self.blobs))
            self.blobs[sha] = payload['content']
            return {'sha': sha}
        if method == 'POST' and path == 'git/trees':
            sha = 'tree' + str(len(self.trees))
            self.trees[sha] = payload['tree']
            return {'sha': sha}
        if method == 'POST' and path == 'git/commits':
            sha = 'commit' + str(len(self.commits))
            self.commits[sha] = {'tree': {'sha': payload['tree']}}
            return {'sha': sha}
        if path == 'git/refs' or path.startswith('git/refs/heads/'):
            assert payload.get('force') is not True
            self.reference = {'object': {'sha': payload['sha']}}
            return self.reference
        raise AssertionError((method, path))


@pytest.fixture
def fixture(tmp_path):
    seed, runtime = tmp_path / 'seed', tmp_path / 'runtime'
    seed.mkdir()
    selection = {'authors': [{'username': 'author'}]}
    state = {'authors': {'author': {'items': [{'id': '1', 'media_type': 'CAROUSEL_ALBUM'}], 'after': 'cursor', 'resume_required': True}}}
    for name, value in [('selection.json', selection), ('state.json', state)]:
        (seed / name).write_text(json.dumps(value))
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'provider': 'meta_graph', 'paid_runs': 0, 'collection': {'target': 1}}))
    return seed, runtime, report


def test_atomic_publish_and_restore_continuation(fixture, tmp_path):
    seed, runtime, report = fixture
    api = GitHub()
    initial = store.load(runtime, seed, api)
    store.save(runtime, report, api)
    restored = store.load(tmp_path / 'next-run', seed, api)
    assert restored == initial
    assert restored['authors']['author']['after'] == 'cursor'
    assert restored['authors']['author']['resume_required']
    files, _ = store.tree_files(api.reference, api)
    assert set(files) == set(store.FILES)
    assert store.read_blob(files['vercel.json'], api)['git']['deploymentEnabled'] is False


def test_racing_publication_does_not_overwrite(fixture):
    seed, runtime, report = fixture
    api = GitHub()
    store.load(runtime, seed, api)
    api.reference = {'object': {'sha': 'another-writer'}}
    with pytest.raises(store.StoreError, match='другим запуском'):
        store.save(runtime, report, api)
    assert not api.commits


def test_no_credentials_in_public_checkpoint(fixture):
    seed, runtime, report = fixture
    api = GitHub()
    store.load(runtime, seed, api)
    (runtime / 'state.json').write_text(json.dumps({'access_token': 'test-placeholder'}))
    with pytest.raises(store.StoreError, match='поле авторизации'):
        store.save(runtime, report, api)
    assert api.reference is None


def test_foreign_branch_is_preserved(fixture):
    seed, runtime, report = fixture
    api = GitHub()
    api.reference = {'object': {'sha': 'other'}}
    api.commits['other'] = {'tree': {'sha': 'other-tree'}}
    api.trees['other-tree'] = [{'path': 'unrelated.json', 'type': 'blob', 'sha': 'unused'}]
    with pytest.raises(store.StoreError, match='другое назначение'):
        store.load(runtime, seed, api)
    assert api.reference['object']['sha'] == 'other'


def test_author_names_are_not_credential_field_names():
    store.validate_data({'authors': {'secret_garden': {'items': []}, 'token_life': {'items': []}}})
    with pytest.raises(store.StoreError):
        store.validate_data({'authors': {'secret_garden': {'access_token': 'placeholder'}}})


def test_workflow_dispatch_accepts_empty_204_response(monkeypatch):
    class Response:
        status = 204
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, *_):
            raise AssertionError('Ответ 204 не содержит JSON')
    monkeypatch.setenv('GH_TOKEN', 'test-only-not-a-real-token')
    monkeypatch.setattr(store, 'urlopen', lambda *_args, **_kwargs: Response())
    assert store.github('POST', 'actions/workflows/authors-activity.yml/dispatches', {'ref': 'main'}) is None
