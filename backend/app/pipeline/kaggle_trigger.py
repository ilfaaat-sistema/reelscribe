from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def trigger_kaggle_notebook() -> bool:
    """Запускает Kaggle-ноутбук через API. Возвращает True если успешно."""
    from app.core.config import settings

    if not (settings.kaggle_api_token and settings.kaggle_notebook_id):
        return False

    import httpx

    notebook_id = settings.kaggle_notebook_id.strip("/")
    if "/" not in notebook_id:
        logger.warning("KAGGLE_NOTEBOOK_ID должен быть вида 'username/notebook-slug'")
        return False

    owner, slug = notebook_id.split("/", 1)
    url = f"https://www.kaggle.com/api/v1/kernels/{owner}/{slug}/run"

    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.kaggle_api_token}"},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("Kaggle notebook triggered: %s", notebook_id)
            return True
        else:
            logger.warning("Kaggle API %s: %s", resp.status_code, resp.text[:300])
            return False
    except Exception as exc:
        logger.warning("Ошибка тригера Kaggle: %s", exc)
        return False
