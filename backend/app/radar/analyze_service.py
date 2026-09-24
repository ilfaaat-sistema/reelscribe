"""Постановка рилсов в очередь на разбор — логика POST /analyze (docs/specs/08-radar.md).

Вынесено из app/api/radar.py: роутер тонкий, решения (что скипнуть, что пропустить через
лимит) принимает сервис (конвенция ReelScribe — SQL/логика не в роутерах).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import repo
from . import settings as radar_settings

if TYPE_CHECKING:
    from app.models.radar_schemas import AnalyzeItem

logger = logging.getLogger(__name__)


class RadarReelNotFoundError(Exception):
    """404 — рилс с таким id не найден в radar_reels."""

    def __init__(self, reel_id: str) -> None:
        self.reel_id = reel_id
        super().__init__(f"Рилс {reel_id} не найден")


class RadarAnalysisDailyLimitError(Exception):
    """429 — превышен суточный лимит новых заданий (ANALYSES_PER_DAY)."""


async def enqueue_analysis(items: list[AnalyzeItem], force: bool) -> dict[str, list[str]]:
    """Валидирует items, решает, что поставить в очередь, а что скипнуть, и создаёт задания.

    Правила skip (без force):
      - у рилса уже есть незавершённое задание (`queued`/`in_progress`) — skip всегда,
        force это не отменяет (иначе одно и то же задание разбиралось бы параллельно);
      - иначе сравниваем запрошенный режим с уже готовыми частями анализа: mode='t' скипаем,
        если есть транскрипт; mode='v' — если есть видеоразбор; mode='tv' — только если есть
        ОБА (частичный анализ, например только транскрипт, для tv не закрывает задачу).
    С force=True второе правило не действует — рилс переставится в очередь, но первое
    (активное задание) действует всегда, повторный /analyze не плодит гонку заданий.

    Возвращает {"queued": [...], "skipped": [...]}. Кидает RadarReelNotFoundError, если
    среди items есть id, которого нет в radar_reels, и RadarAnalysisDailyLimitError при
    превышении суточного лимита.
    """
    reel_ids = [item.reel_id for item in items]
    existing_reels = repo.get_reels_by_ids(reel_ids)
    existing_analyses = repo.get_analyses_by_ids(reel_ids)
    active_job_ids = repo.get_active_job_reel_ids(reel_ids)

    to_queue: list[AnalyzeItem] = []
    skipped: list[str] = []
    for item in items:
        if item.reel_id not in existing_reels:
            raise RadarReelNotFoundError(item.reel_id)

        if item.reel_id in active_job_ids:
            skipped.append(item.reel_id)
            continue

        an = existing_analyses.get(item.reel_id)
        if not force and an is not None:
            has_t = bool(an.get("transcript_segments"))
            has_v = bool(an.get("visual_timeline"))
            closed = (
                (item.mode == "t" and has_t)
                or (item.mode == "v" and has_v)
                or (item.mode == "tv" and has_t and has_v)
            )
            if closed:
                skipped.append(item.reel_id)
                continue
        to_queue.append(item)

    if repo.count_jobs_created_today() + len(to_queue) > radar_settings.ANALYSES_PER_DAY:
        raise RadarAnalysisDailyLimitError("Суточный лимит разбора исчерпан, попробуйте завтра")

    queued: list[str] = []
    for item in to_queue:
        repo.upsert_analysis_queued(item.reel_id, item.mode)
        repo.create_job(item.reel_id, item.mode)
        queued.append(item.reel_id)

    if queued:
        # Лениво: app.radar.pipeline (агент B) заранее освежает протухшие video_url одним
        # вызовом Apify. Сбой не должен ронять постановку в очередь — залогировать и продолжить.
        try:
            from app.radar.pipeline import refresh_expired_links
            await refresh_expired_links(queued)
        except Exception:
            logger.warning("refresh_expired_links не удался для %s", queued, exc_info=True)

    return {"queued": queued, "skipped": skipped}
