"""Настройки Радара.

Живут отдельно от app/core/config.py намеренно: config.py параллельно правит модуль scout,
а Радару нужно всего несколько своих полей. Общие вещи (Supabase, токены Apify) берутся
из основного конфига — `core`.
"""
from __future__ import annotations

import os

from app.core.config import (
    settings as core,  # noqa: F401 — реэкспорт для модулей Радара
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("RADAR_GEMINI_MODEL", "gemini-2.5-flash")

# Лимиты — защита кошелька: эндпоинты публичные, auth в проекте нет. Считаются по базе.
MAX_USERNAMES = 5
MAX_PERIOD_MONTHS = 12
SCRAPES_PER_HOUR = 6
ANALYSES_PER_DAY = 100

# Оценка стоимости сбора (как в локальном Радаре)
REELS_PER_MONTH_ESTIMATE = 20
APIFY_COST_PER_REEL = 0.0026

REEL_ACTOR = "apify~instagram-reel-scraper"
PROFILE_ACTOR = "apify~instagram-profile-scraper"

# Страховка pg_cron → /api/radar/jobs/tick
STALE_QUEUED_SEC = 120    # queued, которое никто не взял — вкладку закрыли
STALE_RUNNING_SEC = 360   # in_progress дольше — функцию убил лимит Vercel
MAX_ATTEMPTS = 3

INLINE_MAX_BYTES = 20 * 1024 * 1024  # до 20 МБ видео уходит в Gemini inline, дальше — Files API
