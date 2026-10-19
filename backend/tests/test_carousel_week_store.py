"""Разные периоды не могут записываться в одну ветку данных."""
import importlib.util
from pathlib import Path


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_weekly_store_is_isolated_from_previous_finished_collection():
    original = module('author_activity_store')
    weekly = module('carousel_week_store')
    assert original.BRANCH == 'data/authors-activity'
    assert weekly._store.BRANCH == 'data/authors-carousels-20261013-20261019'
    assert original.MARKER != weekly._store.MARKER
    assert original.MARKER == {'kind': 'reelscribe-author-activity', 'version': 1}
    assert weekly.load.__globals__['BRANCH'] != original.BRANCH
