"""Изолированное хранилище недельного сбора; прежняя ветка статистики не меняется."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('weekly_checkpoint_store', Path(__file__).with_name('author_activity_store.py'))
_store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_store)
_store.BRANCH = 'data/authors-carousels-20261013-20261019'
_store.MARKER = {'kind': 'reelscribe-carousels-week', 'version': 1}
load = _store.load
save = _store.save
github = _store.github
