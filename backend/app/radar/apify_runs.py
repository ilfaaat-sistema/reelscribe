"""Асинхронный Apify для Радара: старт прогона, опрос статуса, получение датасета.

В отличие от app/pipeline/apify_client.py (run-sync-get-dataset-items — актор запускается и
API-вызов ждёт его целиком), Радару нужен именно АСИНХРОННЫЙ запуск: сбор рилсов может занять
дольше, чем разрешает один запрос serverless-функции Vercel, поэтому прогон стартует одним
вызовом (POST .../runs) и дальше опрашивается короткими запросами — GET /api/radar/scrape/{id}
на фронте (см. docs/specs/08-radar.md, раздел «Архитектура»).

Ротацию токенов и cooldown (исчерпание месячного кредита/точечный бан actor'а) переиспользуем
из app.pipeline.apify_client импортом — сам файл НЕ трогаем и логику не копируем.

REST-пути сверены с docs.apify.com (WebFetch 2026-09-24): текущий префикс — `/v2/actors/`,
устаревший `/v2/acts/` полностью рабочий и ведёт на тот же обработчик. Используем `/v2/acts/`
для единообразия с уже существующим app/pipeline/apify_client.py (там та же база и тот же
префикс для run-sync-get-dataset-items).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.pipeline import shared_state
from app.pipeline.apify_client import (
    ApifyExhaustedError,
    _available_tokens,
    _classify_exhaustion,
    _cool_down_account,
    _cool_down_actor,
)

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"


class ApifyRunError(Exception):
    """Прогон Apify завершился ошибкой (HTTP при старте/опросе, либо FAILED/ABORTED/TIMED-OUT)."""


def _resolve_token(token_ref: str) -> str:
    """token_ref (хэш) → реальный токен. Опрос прогона обязан идти ТЕМ ЖЕ аккаунтом, которым
    он стартован (датасет и run принадлежат конкретному токену Apify)."""
    for token in settings.apify_token_list:
        if shared_state.key_ref(token) == token_ref:
            return token
    raise ApifyRunError("Apify: токен, которым запущен прогон, сейчас недоступен")


async def start_run(actor: str, run_input: dict[str, Any]) -> dict[str, str]:
    """Запускает актор асинхронно (POST /v2/acts/{actor}/runs) с ротацией токенов.

    Возвращает {run_id, dataset_id, token_ref}. token_ref — хэш токена (см. shared_state.key_ref),
    именно он кладётся в radar_scrape_runs.apify_token_ref: сам токен в базу не попадает.
    """
    tokens = await _available_tokens(actor)
    if not tokens:
        raise ApifyExhaustedError(f"Apify: все токены на cooldown для actor {actor}")

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for token in tokens:
            try:
                resp = await client.post(
                    f"{_APIFY_BASE}/acts/{actor}/runs",
                    params={"token": token},
                    json=run_input,
                )
            except Exception as exc:  # noqa: BLE001 — сетевой сбой, пробуем следующий токен
                last_exc = exc
                logger.warning("Apify: старт %s не удался (%s), следующий токен", actor, exc)
                continue

            if resp.status_code >= 400:
                kind = _classify_exhaustion(resp.status_code, resp.text)
                if kind == "account":
                    await _cool_down_account(token)
                    logger.warning("Apify: токен …%s исчерпан (старт %s)", token[-6:], actor)
                    continue
                if kind == "actor":
                    await _cool_down_actor(token, actor)
                    logger.warning("Apify: токен …%s без прав на %s", token[-6:], actor)
                    continue
                last_exc = ApifyRunError(
                    f"Apify {actor}: старт прогона HTTP {resp.status_code} {resp.text[:200]}"
                )
                continue

            data = (resp.json() or {}).get("data") or {}
            run_id = data.get("id")
            dataset_id = data.get("defaultDatasetId")
            if not run_id or not dataset_id:
                last_exc = ApifyRunError(
                    f"Apify {actor}: в ответе нет id/defaultDatasetId: {str(data)[:200]}"
                )
                continue

            return {
                "run_id": run_id,
                "dataset_id": dataset_id,
                "token_ref": shared_state.key_ref(token),
            }

    if last_exc is not None:
        raise ApifyRunError(str(last_exc)) from last_exc
    raise ApifyExhaustedError(f"Apify: не удалось запустить {actor} ни одним токеном")


async def get_run(run_id: str, token_ref: str) -> dict[str, Any]:
    """GET /v2/actor-runs/{runId} — текущий статус прогона."""
    token = _resolve_token(token_ref)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
        try:
            resp = await client.get(f"{_APIFY_BASE}/actor-runs/{run_id}", params={"token": token})
        except Exception as exc:
            raise ApifyRunError(f"Apify: опрос прогона {run_id} не удался ({exc})") from exc

    if resp.status_code >= 400:
        raise ApifyRunError(f"Apify: опрос прогона {run_id} HTTP {resp.status_code} {resp.text[:200]}")

    data = (resp.json() or {}).get("data") or {}
    return {"status": data.get("status"), "dataset_id": data.get("defaultDatasetId")}


async def get_items(dataset_id: str, token_ref: str) -> list[dict[str, Any]]:
    """GET /v2/datasets/{datasetId}/items — все элементы датасета."""
    token = _resolve_token(token_ref)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        try:
            resp = await client.get(
                f"{_APIFY_BASE}/datasets/{dataset_id}/items",
                params={"token": token, "clean": "true"},
            )
        except Exception as exc:
            raise ApifyRunError(f"Apify: получение датасета {dataset_id} не удалось ({exc})") from exc

    if resp.status_code >= 400:
        raise ApifyRunError(
            f"Apify: получение датасета {dataset_id} HTTP {resp.status_code} {resp.text[:200]}"
        )

    items = resp.json()
    if not isinstance(items, list):
        raise ApifyRunError(f"Apify: неожиданный ответ датасета {dataset_id}: {str(items)[:200]}")
    return items
