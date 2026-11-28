"""Короткие запросы Apify для получения списка подписок."""
from __future__ import annotations

import re
from decimal import ROUND_UP, Decimal
from typing import Any, Self

import httpx

LIST_ACTOR = "coregent~instagram-followers-following-scraper"
FREE_MONTHLY_CREDIT = Decimal(5)
# Ставка Free для базовой строки связи из карточки актора.
LIST_RATE = Decimal("0.00125")
_CREDIT_RESERVE = Decimal("0.02")
_API_BASE = "https://api.apify.com/v2/"
_ID_RE = re.compile(r"[A-Za-z0-9]{1,64}")
_PAGE_SIZE = 250
LIST_FIELDS = (
    "rowType,status,coverageStatus,relationshipType,input,scrapedAt,"
    "sourceProfile.username,sourceProfile.followingCount,user.id,user.username,user.fullName,user.isPrivate,"
    "coverage.listCount,coverage.rowsReturned,coverage.stopReason"
)


class ProviderError(Exception):
    """Ответ поставщика не позволяет продолжить операцию."""

    def __init__(self, status: int | None = None):
        self.status = status

    @property
    def rejected(self) -> bool:
        return self.status in {400, 401, 402, 403, 404, 422, 429}


def stage_charge(rate: Decimal, count: int) -> Decimal:
    # Округляем вверх до копеек в единицах API Apify: поставщик применяет жёсткий предел.
    return (rate * count).quantize(Decimal("0.01"), rounding=ROUND_UP)


def _amount(value: Any) -> Decimal:
    if type(value) not in (int, float):
        raise ProviderError()
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ProviderError()
    return amount


def provider_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProviderError()
    return value


class Apify:
    def __init__(self, token: str):
        self.client = httpx.Client(
            base_url=_API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise ProviderError() from None
        if not response.is_success:
            raise ProviderError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise ProviderError() from None

    def _data(self, method: str, path: str, **kwargs: Any) -> dict:
        body = self._request(method, path, **kwargs)
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise ProviderError()
        return body["data"]

    def remaining_credit(self) -> Decimal:
        limits = self._data("GET", "users/me/limits")
        usage = self._data("GET", "users/me/usage/monthly")
        try:
            limit = _amount(limits["limits"]["maxMonthlyUsageUsd"])
            current = _amount(limits["current"]["monthlyUsageUsd"])
            fresh = _amount(usage["totalUsageCreditsUsdAfterVolumeDiscount"])
        except (KeyError, TypeError):
            raise ProviderError() from None
        return min(limit, FREE_MONTHLY_CREDIT) - max(current, fresh) - _CREDIT_RESERVE

    def start(self, actor: str, payload: dict, charge: Decimal) -> str:
        run = self._data(
            "POST",
            f"actors/{actor}/runs",
            params={"maxTotalChargeUsd": str(charge), "timeout": 600, "restartOnError": "false"},
            json=payload,
        )
        return provider_id(run.get("id"))

    def run(self, run_id: str) -> dict:
        return self._data("GET", f"actor-runs/{provider_id(run_id)}")

    def items(self, dataset_id: str, fields: str, max_items: int) -> list[dict]:
        result = []
        for offset in range(0, max_items + 1, _PAGE_SIZE):
            limit = min(_PAGE_SIZE, max_items + 1 - offset)
            page = self._request(
                "GET",
                f"datasets/{provider_id(dataset_id)}/items",
                params={"offset": offset, "limit": limit, "fields": fields},
            )
            if not isinstance(page, list) or len(page) > limit or any(not isinstance(row, dict) for row in page):
                raise ProviderError()
            result.extend(page)
            if len(result) > max_items:
                raise ProviderError()
            if len(page) < limit:
                break
        return result
