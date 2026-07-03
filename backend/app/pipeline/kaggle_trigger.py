from __future__ import annotations

import logging
import os
import tempfile
import json
from pathlib import Path

logger = logging.getLogger(__name__)

_NOTEBOOK_SRC = Path(__file__).parents[4] / 'kaggle' / 'transcribe_batch.ipynb'


def trigger_kaggle_notebook() -> bool:
    """Запускает Kaggle-ноутбук через kaggle-api. Возвращает True если успешно."""
    from app.core.config import settings

    if not (settings.kaggle_key and settings.kaggle_notebook_id):
        return False

    try:
        os.environ['KAGGLE_USERNAME'] = settings.kaggle_username
        os.environ['KAGGLE_KEY'] = settings.kaggle_key

        import kaggle
        kaggle.api.authenticate()

        owner, slug = settings.kaggle_notebook_id.strip('/').split('/', 1)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # копируем ноутбук
            if not _NOTEBOOK_SRC.exists():
                logger.warning(
                    'Ноутбук %s не найден — Kaggle-кернел не тронут, тригер пропущен',
                    _NOTEBOOK_SRC,
                )
                return False

            import shutil
            shutil.copy(_NOTEBOOK_SRC, tmp_path / 'transcribe_batch.ipynb')
            nb_file = 'transcribe_batch.ipynb'

            # метаданные для kernels/push
            meta = {
                'id': f'{owner}/{slug}',
                'title': 'ReelScribe Batch Transcription',
                'code_file': nb_file,
                'language': 'python',
                'kernel_type': 'notebook',
                'is_private': 'true',
                'enable_gpu': 'true',
                'enable_internet': 'true',
                'dataset_sources': [],
                'competition_sources': [],
                'kernel_sources': [],
            }
            (tmp_path / 'kernel-metadata.json').write_text(json.dumps(meta))

            kaggle.api.kernels_push(str(tmp_path))

        logger.info('Kaggle notebook triggered: %s', settings.kaggle_notebook_id)
        return True

    except Exception as exc:
        logger.warning('Ошибка тригера Kaggle: %s', exc)
        return False
