from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def trigger_worker_run() -> bool:
    """Мгновенно запускает воркер на GitHub Actions через repository_dispatch.

    Best-effort: любой сбой (нет токена, сеть, GitHub API недоступен) только
    логируется — сбой тригера не имеет права уронить импорт/ретрай пользователя.
    Без тригера задание всё равно подхватится по расписанию (cron */5 * * * *).
    """
    from app.core.config import settings

    if not (settings.github_dispatch_token and settings.github_dispatch_repo):
        return False

    try:
        import httpx

        resp = httpx.post(
            f'https://api.github.com/repos/{settings.github_dispatch_repo}/dispatches',
            headers={
                'Authorization': f'Bearer {settings.github_dispatch_token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            json={'event_type': settings.github_dispatch_event_type},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info('GitHub Actions worker triggered: %s', settings.github_dispatch_repo)
        return True

    except Exception as exc:
        logger.warning('Ошибка тригера GitHub Actions: %s', exc)
        return False
