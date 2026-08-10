"""Общий клиент Apify с ротацией токенов + получение подписчиков.

Ротация: у бесплатного тарифа Apify месячный кредит (~$5). Когда у одного токена он кончается,
Apify отвечает ошибкой лимита — тогда «остужаем» этот токен и переключаемся на следующий
(как ротация RapidAPI-ключей в starapi_downloader). Все Apify-вызовы (профиль/аудио/подписчики)
ходят через run_actor_get_items, поэтому ротация действует на весь каскад.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"


class ApifyExhaustedError(Exception):
    """Все Apify-токены исчерпали кредит/лимит (или на cooldown) для запрошенного actor'а."""


# Токены, забаненные целиком (кредит аккаунта исчерпан) — на ВСЕ actor'ы.
# {token: monotonic-время, когда снова можно пробовать}.
_cooldown: dict[str, float] = {}

# Токены, забаненные точечно — только для конкретного actor'а (нет прав/аренды на платный actor,
# но дешёвый actor того же токена работает нормально). {(token, actor): monotonic-время}.
_actor_cooldown: dict[tuple[str, str], float] = {}


def _available_tokens(actor: str) -> list[str]:
    now = time.monotonic()
    return [
        t for t in settings.apify_token_list
        if _cooldown.get(t, 0.0) <= now and _actor_cooldown.get((t, actor), 0.0) <= now
    ]


def _cool_down_account(token: str) -> None:
    _cooldown[token] = time.monotonic() + settings.apify_token_cooldown_min * 60


def _cool_down_actor(token: str, actor: str) -> None:
    _actor_cooldown[(token, actor)] = time.monotonic() + settings.apify_token_cooldown_min * 60


_RETRIES_PER_TOKEN = 3   # ретраи транзиентных ошибок (429/5xx/временный 403) на одном токене


def _classify_exhaustion(status: int, body: str) -> str | None:
    """Классифицирует ошибку HTTP-ответа: 'account' | 'actor' | None (не про исчерпание).

    Транзиентный 403 (мгновенный лимит на старт actor-ранов) сюда НЕ попадает — иначе один блип
    забанил бы хороший токен на cooldown.

    - 'account' — исчерпан месячный кредит/лимит АККАУНТА: банить токен целиком, для всех actor'ов.
      Apify шлёт 'Monthly usage hard limit exceeded' и подобные формулировки в теле.
    - 'actor' — нет прав/аренды на КОНКРЕТНЫЙ платный actor (напр. profile-scraper без покупки):
      банить только пару (токен, actor) — остальные actor'ы этим токеном пользоваться могут.
      Apify отвечает 'platform-feature-disabled' в теле либо HTTP 402 (оплата).
    """
    low = body.lower()
    if any(k in low for k in (
        "monthly usage", "hard limit", "usage-hard-limit", "usage hard limit", "monthly-usage",
    )):
        return "account"
    if status == 402 or "platform-feature-disabled" in low:
        return "actor"
    return None


async def run_actor_get_items(
    client: httpx.AsyncClient,
    actor: str,
    run_input: dict[str, Any],
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """POST run-sync-get-dataset-items с ротацией токенов + ретраями транзиентных ошибок."""
    tokens = _available_tokens(actor)
    if not tokens:
        raise ApifyExhaustedError(f"Apify: все токены на cooldown для actor {actor}")

    last_exc: Exception | None = None
    for token in tokens:
        params: dict[str, Any] = {"token": token}
        if extra_params:
            params.update(extra_params)

        benched = False
        for attempt in range(_RETRIES_PER_TOKEN):
            try:
                resp = await client.post(
                    f"{_APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items",
                    params=params,
                    json=run_input,
                )
            except Exception as exc:  # noqa: BLE001 — сетевой сбой, ретрай тем же токеном
                last_exc = exc
                await asyncio.sleep(2.0 * (attempt + 1))
                continue

            if resp.status_code >= 400:
                # Классификацию исчерпания смотрим ТОЛЬКО на ошибочных ответах — тело успешного
                # (HTTP 200) ответа это выдача Instagram (caption постов и т.п.), и совпадение
                # ключевых слов там — ложное срабатывание, а не сигнал об исчерпании квоты.
                kind = _classify_exhaustion(resp.status_code, resp.text)
                if kind == "account":
                    _cool_down_account(token)
                    logger.warning(
                        "Apify: токен …%s — кредит аккаунта исчерпан (HTTP %s), переключаюсь на следующий",
                        token[-6:], resp.status_code,
                    )
                    benched = True
                    break
                if kind == "actor":
                    _cool_down_actor(token, actor)
                    logger.warning(
                        "Apify: токен …%s — нет прав на actor %s (HTTP %s), переключаюсь на следующий",
                        token[-6:], actor, resp.status_code,
                    )
                    benched = True
                    break

                # Транзиентная ошибка (429/5xx/временный 403) — ретрай ТЕМ ЖЕ токеном с бэкоффом.
                last_exc = RuntimeError(f"Apify {actor}: HTTP {resp.status_code} {resp.text[:120]}")
                logger.warning(
                    "Apify: токен …%s HTTP %s (попытка %d/%d) — ретрай",
                    token[-6:], resp.status_code, attempt + 1, _RETRIES_PER_TOKEN,
                )
                await asyncio.sleep(3.0 * (attempt + 1))
                continue

            items = resp.json()
            if not isinstance(items, list):
                raise RuntimeError(f"Apify {actor}: неожиданный ответ {str(items)[:120]}")
            return items

        if benched:
            continue   # токен исчерпан — следующий токен
        # ретраи на этом токене не помогли (транзиент) — пробуем следующий токен, если есть

    if last_exc is not None:
        raise last_exc
    raise ApifyExhaustedError(f"Apify: все токены исчерпали кредит/лимит для actor {actor}")


# ── Подписчики через профиль-актор (followersCount) ──────────────────────────────────────────
# Кэш: username → (count, expires_at). Успех (count не None) кэшируется БЕССРОЧНО (expires_at=None) —
# подписчики не меняются достаточно быстро, чтобы платить за них повторно. Неудача (count=None,
# например все токены были на cooldown) кэшируется с TTL = cooldown токенов: имеет смысл повторить
# попытку ровно тогда, когда токены снова доступны, а не молчать про этого автора вечно.
_followers_cache: dict[str, tuple[int | None, float | None]] = {}
_MISS = object()   # сентинел «в кэше ничего нет / запись протухла» (отличим от закэшированного None)

# Per-username локи + общий guard, который их создаёт (тот же приём, что в
# apify_profile_downloader._guard/_profile_lock) — блокировка держится на всё время сетевого
# запроса, а не только на чтение/запись кэша, поэтому параллельные джобы одного автора не
# запускают дублирующиеся ПЛАТНЫЕ profile-scraper run'ы.
_followers_locks: dict[str, asyncio.Lock] = {}
_locks_guard: asyncio.Lock | None = None
_guard_loop: asyncio.AbstractEventLoop | None = None


def _guard() -> asyncio.Lock:
    global _locks_guard, _guard_loop
    loop = asyncio.get_running_loop()
    if _locks_guard is None or _guard_loop is not loop:
        _locks_guard = asyncio.Lock()
        _followers_locks.clear()
        _guard_loop = loop
    return _locks_guard


async def _followers_lock_for(username: str) -> asyncio.Lock:
    async with _guard():
        lock = _followers_locks.get(username)
        if lock is None:
            lock = asyncio.Lock()
            _followers_locks[username] = lock
        return lock


def _get_cached_followers(username: str) -> Any:
    """Значение из кэша, либо `_MISS`, если записи нет или истёк TTL отрицательного результата."""
    entry = _followers_cache.get(username)
    if entry is None:
        return _MISS
    count, expires_at = entry
    if expires_at is not None and time.monotonic() >= expires_at:
        return _MISS
    return count


def _set_cached_followers(username: str, count: int | None) -> None:
    if count is not None:
        _followers_cache[username] = (count, None)   # успех — бессрочно
    else:
        ttl = settings.apify_token_cooldown_min * 60
        _followers_cache[username] = (None, time.monotonic() + ttl)   # неудача — TTL до cooldown


async def get_followers_via_apify(username: str | None) -> int | None:
    """Число подписчиков автора через apify~instagram-profile-scraper. Кэш по username.

    Любой сбой (нет токенов/исчерпан кредит/профиль не найден) → None: рилс не валим, просто
    остаётся без подписчиков (метрики er/lpf/cpf по нему не посчитаются — это лучше, чем падение).
    """
    if not username or not settings.apify_token_list:
        return None

    # Лок держим на весь запрос (не только на чтение/запись кэша) — параллельные джобы одного
    # автора ждут первый запрос и берут результат из кэша, а не плодят платные run'ы каждый свой.
    lock = await _followers_lock_for(username)
    async with lock:
        cached = _get_cached_followers(username)
        if cached is not _MISS:
            return cached

        count: int | None = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                items = await run_actor_get_items(
                    client, settings.apify_profile_info_actor, {"usernames": [username]},
                )
            if items:
                count = items[0].get("followersCount")
                logger.info("Apify профиль ✓ @%s → %s подписчиков", username, count)
        except ApifyExhaustedError as exc:
            logger.warning("Apify: подписчиков @%s не получить (%s)", username, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apify: профиль @%s ошибка (%s)", username, exc)

        _set_cached_followers(username, count)
        return count
