"""Константы расчёта разведки Радара (docs/specs/10-radar-intel.md, раздел «Расчёт»)."""
from __future__ import annotations

# Залётность: порог бейджа и уровни (v3 / v5 / v10 на фронте).
VIRAL_RATIO_THRESHOLD = 3.0
VIRAL_LEVELS = (3.0, 5.0, 10.0)

# Норма аккаунта: медиана просмотров по «созревшим» рилсам.
MATURE_DAYS = 3
NORM_WINDOW_DAYS = 90
NORM_MAX_REELS = 60
NORM_MIN_N = 5          # меньше — залётность не считается (null)
NORM_RELIABLE_N = 10    # от этого числа норма «надёжная», бейдж без пометки «мало данных»

# «Набирает».
RISING_MIN_GAP_HOURS = 20
RISING_RATIO = 1.2
RISING_MAX_AGE_DAYS = 14
RISING_NOHISTORY_MAX_AGE_DAYS = 7
RISING_PERCENTILE = 0.7

# Лимит отслеживаемых источников.
MAX_SOURCES = 60
