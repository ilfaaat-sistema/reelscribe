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
# /usage/monthly отдаёт totalUsageCreditsUsdAfterVolumeDiscount — тот же расход, что и
# monthlyUsageUsd в /limits, но без задержки агрегации в несколько минут. Заведено 04.09.2026:
# /limits показывал $0.00 при реальных $0.0027 и по нему решили, что Apify вообще не вызывался.
_USAGE_URL = "https://api.apify.com/v2/users/me/usage/monthly"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _check_token(index: int, token: str) -> float:
    """Запрашивает лимиты и актуальный расход одного токена, печатает строку, возвращает остаток в $.

    Токен в вывод НИКОГДА не попадает — ни в успешной строке, ни в тексте ошибки: он живёт
    только в заголовке Authorization, в URL не передаётся.

    Остаток считается от свежей цифры (/usage/monthly), если она доступна — иначе от той,
    что дал /limits (с задержкой агрегации).
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(_LIMITS_URL, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {})
        used_delayed = float(data.get("current", {}).get("monthlyUsageUsd") or 0.0)
        limit = float(data.get("limits", {}).get("maxMonthlyUsageUsd") or 0.0)
    except Exception as exc:  # noqa: BLE001 — любая ошибка одного токена не должна валить остальные
        # Текст исключения может содержать URL — токен туда не попадает (он в заголовке), но на
        # всякий случай обрезаем до 120 символов, чтобы длинный трейс не засорял вывод.
        text = str(exc)[:120]
        print(f"токен #{index}: ошибка {type(exc).__name__} {text}")
        return 0.0

    used_fresh = None
    try:
        resp = httpx.get(_USAGE_URL, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        usage_data = resp.json().get("data", {})
        used_fresh = float(usage_data.get("totalUsageCreditsUsdAfterVolumeDiscount") or 0.0)
    except Exception as exc:  # noqa: BLE001 — свежая цифра опциональна, без неё работаем со старой
        text = str(exc)[:120]
        print(f"токен #{index}: /usage/monthly не ответил ({type(exc).__name__} {text}), только отстающая цифра")

    if used_fresh is not None:
        remaining = round(limit - used_fresh, 2)
        print(
            f"токен #{index}: потрачено ${round(used_fresh, 2):.2f} (свежее) / "
            f"${round(used_delayed, 2):.2f} (с задержкой) из ${round(limit, 2):.2f} "
            f"— остаток ${remaining:.2f}"
        )
    else:
        remaining = round(limit - used_delayed, 2)
        print(
            f"токен #{index}: потрачено ${round(used_delayed, 2):.2f} (с задержкой, свежая цифра "
            f"недоступна) из ${round(limit, 2):.2f} — остаток ${remaining:.2f}"
        )
    return remaining


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
