"""История прогонов воркера в `public.worker_runs` — best-effort, как shared_state.py.

Зачем модуль нужен: до 04.09.2026 на вопрос «чем скачаны рилсы этого прогона» и «во что он
обошёлся» ответить было нечем — источник скачивания жил только в памяти процесса, а история
прогонов — только в логах GitHub Actions (доступны лишь после завершения прогона, живут 90 дней).
Миграция `supabase/migrations/0004_observability.sql` добавляет таблицу `worker_runs` и колонку
`transcripts.source`; этот модуль читает и пишет их.

**Всё здесь best-effort**, как и в shared_state.py: разбор очереди не должен упасть или
замедлиться из-за того, что миграция ещё не применена или база на секунду недоступна. Любая
ошибка — warning в лог и тихий выход. Функции возвращают `None`/ничего не делают, вызывающий
код (app/workers/run.py) продолжает работать как будто истории прогонов не существует.

**Определение недоступности:** первая же неудача записи выставляет модульный флаг
`_unavailable` — дальше модуль молча не делает запросов вообще, чтобы не долбить базу и не
засорять лог повторными предупреждениями на каждом рилсе. Проблема логируется один раз.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from app.core.db import get_db

logger = logging.getLogger(__name__)

_TABLE = 'worker_runs'

# Как только запись/чтение таблицы прогонов один раз не удалась (таблицы нет, миграция не
# применена, база недоступна) — считаем её недоступной до конца процесса и больше не пытаемся.
_unavailable = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_unavailable(action: str, exc: Exception) -> None:
    global _unavailable
    if not _unavailable:
        logger.warning(
            'История прогонов (worker_runs) недоступна — %s не удалось (%s). '
            'Дальше в этом прогоне запись/обновление истории пропускается, разбор очереди продолжается.',
            action, exc,
        )
    _unavailable = True


def start_run(note: Optional[str] = None) -> Optional[str]:
    """Создаёт строку прогона в worker_runs, возвращает её id или None при любой ошибке.

    run_id берётся из переменной окружения GITHUB_RUN_ID (прогон на Actions); локально её нет —
    тогда run_id остаётся None, это ожидаемо.
    """
    if _unavailable:
        return None
    try:
        db = get_db()
        row = {
            'run_id': os.environ.get('GITHUB_RUN_ID') or None,
            'started_at': _now_iso(),
            'updated_at': _now_iso(),
            'note': note,
        }
        resp = db.table(_TABLE).insert(row).execute()
        if not resp.data:
            return None
        return resp.data[0].get('id')
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable('создание строки прогона', exc)
        return None


def update_run(
    run_uuid: Optional[str],
    *,
    sources: dict,
    jobs_done: int,
    jobs_no_audio: int,
    jobs_failed: int,
) -> None:
    """Обновляет счётчики прогона по ходу разбора. Дешёвый и тихий вызов — периодически из run.py."""
    if run_uuid is None or _unavailable:
        return
    try:
        db = get_db()
        db.table(_TABLE).update({
            'updated_at': _now_iso(),
            'jobs_done': jobs_done,
            'jobs_no_audio': jobs_no_audio,
            'jobs_failed': jobs_failed,
            'sources': dict(sources),
        }).eq('id', run_uuid).execute()
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable('обновление строки прогона', exc)


def finish_run(
    run_uuid: Optional[str],
    *,
    outcome: str,
    sources: dict,
    jobs_done: int,
    jobs_no_audio: int,
    jobs_failed: int,
    apify_spent_usd: Optional[float],
) -> None:
    """Закрывает строку прогона: finished_at, outcome и итоговые цифры."""
    if run_uuid is None or _unavailable:
        return
    try:
        db = get_db()
        db.table(_TABLE).update({
            'updated_at': _now_iso(),
            'finished_at': _now_iso(),
            'outcome': outcome,
            'jobs_done': jobs_done,
            'jobs_no_audio': jobs_no_audio,
            'jobs_failed': jobs_failed,
            'sources': dict(sources),
            'apify_spent_usd': apify_spent_usd,
        }).eq('id', run_uuid).execute()
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable('закрытие строки прогона', exc)


def apify_spent_usd_safe() -> Optional[float]:
    """Обёртка над apify_quota.total_spent_usd(): None при любой ошибке, разбор не должен встать."""
    try:
        from app.workers.apify_quota import total_spent_usd
        return total_spent_usd()
    except Exception as exc:  # noqa: BLE001
        logger.warning('Расход Apify не снят (%s) — apify_spent_usd прогона останется пустым', exc)
        return None
