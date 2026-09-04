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
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
# /usage/monthly отдаёт totalUsageCreditsUsdAfterVolumeDiscount — тот же расход, что и
# monthlyUsageUsd в /limits, но без задержки агрегации в несколько минут. Заведено 04.09.2026:
# /limits показывал $0.00 при реальных $0.0027 и по нему решили, что Apify вообще не вызывался.
_USAGE_URL = "https://api.apify.com/v2/users/me/usage/monthly"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _fresh_usage_usd(token: str) -> Optional[float]:
    """Свежий расход одного токена по /usage/monthly, либо None при любой ошибке.

    Вынесено отдельно, чтобы использоваться и датчиком остатка (_check_token), и
    total_spent_usd() — снимком расхода для истории прогонов воркера (app/workers/run_log.py).
    """
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(_USAGE_URL, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    usage_data = resp.json().get("data", {})
    return float(usage_data.get("totalUsageCreditsUsdAfterVolumeDiscount") or 0.0)


def total_spent_usd() -> Optional[float]:
    """Суммарный расход Apify за месяц по всем токенам ротации, либо None, если снять не удалось.

    Используется для истории прогонов воркера: снимок в начале и в конце, разница = цена прогона.
    None — если токены не настроены или НИ ОДИН не ответил (частичный сбой одних токенов при
    успехе других не считается провалом — суммируем то, что получили).
    """
    tokens = settings.apify_token_list
    if not tokens:
        return None
    total = 0.0
    got_any = False
    for token in tokens:
        try:
            total += _fresh_usage_usd(token)
            got_any = True
        except Exception as exc:  # noqa: BLE001 — один неответивший токен не должен срывать снимок
            logger.warning("Apify usage: токен не ответил (%s)", type(exc).__name__)
    return round(total, 4) if got_any else None


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
        used_fresh = _fresh_usage_usd(token)
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
