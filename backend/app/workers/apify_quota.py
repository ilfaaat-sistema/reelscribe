"""Датчик остатка бесплатной месячной квоты Apify по всем токенам ротации.

У бесплатного тарифа Apify месячный кредит (~$5) на аккаунт — общий для всех API-токенов
этого аккаунта (см. `apify_token_list` в app/core/config.py). Когда токен исчерпан, каскад
скачивания переключается на следующий (app/pipeline/apify_client.py). Этот скрипт не трогает
пайплайн, только опрашивает Apify API и печатает остаток по каждому токену — чтобы не гадать,
скоро ли ротация упрётся в потолок у всех разом.

Запуск:
    python -m app.workers.apify_quota
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _check_token(index: int, token: str) -> float:
    """Запрашивает лимиты одного токена, печатает строку, возвращает остаток в $ (0.0 при ошибке).

    Токен в вывод НИКОГДА не попадает — ни в успешной строке, ни в тексте ошибки: он живёт
    только в заголовке Authorization, в URL не передаётся.
    """
    try:
        resp = httpx.get(
            _LIMITS_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {})
        used = float(data.get("current", {}).get("monthlyUsageUsd") or 0.0)
        limit = float(data.get("limits", {}).get("maxMonthlyUsageUsd") or 0.0)
        remaining = round(limit - used, 2)
        print(
            f"токен #{index}: потрачено ${round(used, 2):.2f} из ${round(limit, 2):.2f} "
            f"— остаток ${remaining:.2f}"
        )
        return remaining
    except Exception as exc:  # noqa: BLE001 — любая ошибка одного токена не должна валить остальные
        # Текст исключения может содержать URL — токен туда не попадает (он в заголовке), но на
        # всякий случай обрезаем до 120 символов, чтобы длинный трейс не засорял вывод.
        text = str(exc)[:120]
        print(f"токен #{index}: ошибка {type(exc).__name__} {text}")
        return 0.0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx логирует каждый запрос строкой INFO — в датчике это шум поверх самих цифр.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tokens = settings.apify_token_list
    if not tokens:
        print("Apify-токены не настроены (APIFY_API_TOKEN / APIFY_API_TOKENS пусты)")
        return

    total_remaining = 0.0
    for i, token in enumerate(tokens, 1):
        total_remaining += _check_token(i, token)

    print(f"Суммарный остаток: ${round(total_remaining, 2):.2f}")


if __name__ == "__main__":
    main()
