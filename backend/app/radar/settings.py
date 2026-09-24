"""Настройки Радара.

Живут отдельно от app/core/config.py намеренно: config.py параллельно правит модуль scout,
а Радару нужно всего несколько своих полей. Общие вещи (Supabase, токены Apify) берутся
из основного конфига — `core`.
"""
from __future__ import annotations

import os

from app.core.config import settings as core

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("RADAR_GEMINI_MODEL", "gemini-2.5-flash")

# Whisper (OpenAI) — ключ общий с остальным ReelScribe (app/pipeline/transcribe.py читает тот
# же OPENAI_API_KEY через core.openai_api_key), не заводим отдельную переменную окружения.
OPENAI_API_KEY = core.openai_api_key
WHISPER_MODEL = os.getenv("RADAR_WHISPER_MODEL", "whisper-1")
WHISPER_MAX_BYTES = 25 * 1024 * 1024  # лимит OpenAI на файл транскрибации

# Лимиты — защита кошелька: эндпоинты публичные, auth в проекте нет. Считаются по базе.
MAX_USERNAMES = 5
MAX_PERIOD_MONTHS = 24  # итерация 2 (24.09.2026): оригинальный слайдер локального Радара 1..24
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
