"""Облачная очередь перевода сохранённых подписей каруселей.

Сеть ограничена тремя сохранёнными GitHub snapshots и фиксированным
production API. Исходники и переводы в checkpoint не записываются.
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.services.carousel_caption_bulk_store import CarouselCaptionBulkStore, Conflict
from app.services.carousel_caption_rules import (
    CAPTION_RULES_VERSION,
    classify_caption,
    source_hash,
)

API_BASE = "https://reelscribe-ai.vercel.app/api"
MAX_RUN_SECONDS = 20 * 60
MAX_POSTS = 500
BATCH_SIZE = 10
SAVE_RESERVE = 20
# Запас покрывает один максимальный POST, GET-reconcile и финальный CAS save.
REQUEST_DRAIN_SECONDS = 360
MOSCOW = ZoneInfo("Europe/Moscow")
PERIOD_PRIORITY = ("30d", "14d", "archive14d", "7d")
SOURCES = {
    "archive7d": ("data/authors-carousels-20261013-20261019", "activity-report.json"),
    "archive14d": ("data/authors-carousels-20261006-20261019", "activity-report.json"),
    "live": ("data/carousels-favorites-live", "state.json"),
}
_ID = re.compile(r"[0-9]{1,40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class ApiFailure(RuntimeError):
    def __init__(self, status: int, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class ProductionApi:
    def __init__(self, client, base: str = API_BASE):
        if base != API_BASE:
            raise ValueError("Разрешён только фиксированный production API")
        self.client = client
        self.base = base

    @staticmethod
    def _retry_after(response) -> int | None:
        try:
            value = int(response.headers.get("retry-after", ""))
        except ValueError:
            return None
        return value if 1 <= value <= 86400 else None

    @staticmethod
    def _json(response) -> dict:
        if len(response.content) > 2 * 1024 * 1024:
            raise ApiFailure(503, "api_response_too_large")
        try:
            value = response.json()
        except ValueError:
            raise ApiFailure(response.status_code, "api_invalid_json") from None
        if not isinstance(value, dict):
            raise ApiFailure(response.status_code, "api_invalid_json")
        return value

    def get(self, item: dict) -> dict:
        mismatched = None
        for period in (period for period in PERIOD_PRIORITY if period in item["periods"]):
            response = self.client.get(
                f"{self.base}/radar/carousels/{item['id']}",
                params={"period": period},
                timeout=30,
            )
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                value = self._json(response)
                code = value.get("code")
                raise ApiFailure(response.status_code, code if isinstance(code, str) else "detail_unavailable",
                                 self._retry_after(response))
            value = self._json(response)
            detail = value.get("caption_translation")
            if not isinstance(detail, dict):
                raise ApiFailure(503, "old_api_contract")
            detail = {**detail, "_period": period}
            if detail.get("source_hash") == item["source_hash"]:
                return detail
            if mismatched is None:
                mismatched = detail
        if mismatched is not None:
            return mismatched
        raise ApiFailure(404, "carousel_not_found")

    def post(self, item: dict, period: str | None = None) -> dict:
        # httpx client создан без transport retries; повтор POST запрещён.
        response = self.client.post(
            f"{self.base}/radar/carousels/{item['id']}/translate",
            json={"period": period or preferred_period(item), "source_hash": item["source_hash"], "automatic": True},
            timeout=75,
        )
        value = self._json(response)
        if response.status_code != 200:
            code = value.get("code")
            raise ApiFailure(response.status_code, code if isinstance(code, str) else "translation_unknown",
                             self._retry_after(response))
        detail = value.get("caption_translation")
        if not isinstance(detail, dict) or detail.get("state") != "ready" or detail.get("source_hash") != item["source_hash"]:
            raise ApiFailure(502, "translation_invalid_success")
        return detail


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_stamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Дата источника без часового пояса")
    return result.astimezone(timezone.utc)


def preferred_period(item: dict) -> str:
    return next(period for period in PERIOD_PRIORITY if period in item["periods"])


def _validate_rows(value: object, source: str) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("Неверный формат snapshot")  # noqa: TRY004 — ошибка внешних данных.
    if source.startswith("archive") and value.get("provider") != "meta_graph":
        raise ValueError("Неверный архивный snapshot")
    if source == "live" and value.get("version") != 1:
        raise ValueError("Неверная версия live snapshot")
    rows = value["items"]
    for row in rows:
        if not isinstance(row, dict) or row.get("media_type") != "CAROUSEL_ALBUM":
            raise ValueError("Неверная запись snapshot")
        identity = str(row.get("id", ""))
        if not _ID.fullmatch(identity) or not isinstance(row.get("caption"), (str, type(None))):
            raise ValueError("Неверные ID/caption snapshot")
        if source == "live":
            if not isinstance(row.get("timestamp"), str):
                raise ValueError("Live snapshot без timestamp")
            parse_stamp(row["timestamp"])
    return rows


def build_inventory(snapshots: dict[str, object], now: datetime) -> tuple[list[dict], dict[str, int]]:
    """Дедупликация по точному ID+UTF-8 hash; текст не покидает функцию."""
    result: dict[tuple[str, str], dict] = {}
    today = now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=1)

    def add(row: dict, periods: list[str]) -> None:
        caption = row.get("caption") or ""
        identity = str(row["id"])
        digest = source_hash(caption)
        key = identity, digest
        entry = result.setdefault(key, {
            "id": identity,
            "source_hash": digest,
            "periods": [],
            "category": classify_caption(caption),
        })
        if entry["category"] != classify_caption(caption):
            raise ValueError("Несогласованный exact hash")
        entry["periods"].extend(period for period in periods if period not in entry["periods"])

    for row in _validate_rows(snapshots["archive7d"], "archive7d"):
        add(row, ["7d"])
    for row in _validate_rows(snapshots["archive14d"], "archive14d"):
        add(row, ["archive14d"])
    for row in _validate_rows(snapshots["live"], "live"):
        value = parse_stamp(row["timestamp"]).astimezone(MOSCOW)
        periods = []
        if today - timedelta(days=29) <= value < end:
            periods.append("30d")
        if today - timedelta(days=13) <= value < end:
            periods.append("14d")
        if periods:
            add(row, periods)
    rows = sorted(result.values(), key=lambda item: (item["id"], item["source_hash"]))
    for item in rows:
        item["periods"] = [period for period in PERIOD_PRIORITY if period in item["periods"]]
    return rows, dict(Counter(item["category"] for item in rows))


def merge_inventory(state: dict, inventory: list[dict], counts: dict[str, int], now: datetime) -> None:
    previous = {(item["id"], item["source_hash"]): item for item in state["items"]}
    merged = []
    current_keys = set()
    for found in inventory:
        key = found["id"], found["source_hash"]
        current_keys.add(key)
        old = previous.get(key)
        category = found["category"]
        if old and old["status"] in {"ready", "inflight", "unknown", "review", "error", "source_changed"}:
            # Новая классификация не стирает историю исхода и не разрешает новый POST.
            status, code = old["status"], old["code"]
            inflight_started_at = old["inflight_started_at"]
        elif category != "eligible":
            status = category
            code = category
            inflight_started_at = None
        elif old:
            status, code = old["status"], old["code"]
            inflight_started_at = old["inflight_started_at"]
            if status in {"empty", "not_needed", "too_long", "gone"}:
                status, code, inflight_started_at = "pending", None, None
        else:
            status, code, inflight_started_at = "pending", None, None
        merged.append({
            "id": found["id"], "source_hash": found["source_hash"], "periods": found["periods"],
            "status": status, "updated_at": old["updated_at"] if old else stamp(now),
            "code": code, "inflight_started_at": inflight_started_at,
        })
    # Не стираем историю попытки из checkpoint, если карточка удалена из snapshots.
    for key, old in previous.items():
        if key not in current_keys:
            removed = deepcopy(old)
            removed.update(status="gone", code="source_removed", updated_at=stamp(now), inflight_started_at=None)
            merged.append(removed)
    state["items"] = sorted(merged, key=lambda item: (item["id"], item["source_hash"]))
    state["inventory"] = {name: counts.get(name, 0) for name in ("empty", "not_needed", "too_long", "eligible")}
    state["updated_at"] = stamp(now)


def discover(store, state: dict, now: datetime) -> bool:
    refs = {}
    for name, (branch, path) in SOURCES.items():
        commit, blob = store.source_ref(branch, path)
        refs[name] = {"commit": commit, "blob": blob}
    discovered_day = now.astimezone(MOSCOW).date().isoformat()
    if (refs == state["refs"] and state["discovered_day"] == discovered_day
            and state.get("classification_version", 1) == CAPTION_RULES_VERSION):
        return False
    snapshots = {
        name: store.read_source_json(branch, path, refs[name]["blob"])
        for name, (branch, path) in SOURCES.items()
    }
    inventory, counts = build_inventory(snapshots, now)
    merge_inventory(state, inventory, counts, now)
    state["refs"] = refs
    state["discovered_day"] = discovered_day
    state["classification_version"] = CAPTION_RULES_VERSION
    return True


def _set(item: dict, status: str, now: datetime, code: str | None = None, *, preserve_inflight: bool = False) -> None:
    item["status"] = status
    item["code"] = code
    item["updated_at"] = stamp(now)
    if not preserve_inflight:
        item["inflight_started_at"] = None


def _attempt(detail: dict) -> tuple[str | None, bool]:
    attempt = detail.get("attempt")
    if not isinstance(attempt, dict):
        return None, False
    status = attempt.get("status")
    if status not in {"none", "running", "expired_unknown", "failed", "quota", "source_changed", "unavailable"}:
        return None, False
    return status, True


def apply_detail(item: dict, detail: dict, now: datetime, *, inflight_recovery: bool = False) -> str:
    digest = detail.get("source_hash")
    state = detail.get("state")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest) or state not in {"ready", "not_needed", "available", "unavailable"}:
        _set(item, "unknown", now, "old_api_contract")
        return "pause_old_api"
    if digest != item["source_hash"]:
        _set(item, "source_changed", now, "source_changed")
        return "source_changed"
    if state == "ready":
        _set(item, "ready", now, "ready")
        return "done"
    if state == "not_needed":
        _set(item, "not_needed", now, "translation_not_needed")
        return "done"
    if state == "unavailable":
        # Следующий run сможет read-only увидеть ready, но не POST.
        _set(item, "inflight", now, "translation_unavailable", preserve_inflight=True)
        return "pause_unavailable"
    attempt, valid = _attempt(detail)
    if not valid:
        # Старый API не доказывает отсутствие платной попытки.
        _set(item, "inflight", now, "old_api_contract", preserve_inflight=True)
        return "pause_old_api"
    if attempt == "none":
        if not inflight_recovery:
            return "post"
        started = parse_stamp(item["inflight_started_at"])
        age = (now - started).total_seconds()
        if 360 <= age < 24 * 3600:
            _set(item, "pending", now, "unsent_recovered")
            return "pending"
        if age >= 24 * 3600:
            _set(item, "review", now, "stale_inflight")
            return "review"
        return "wait"
    if attempt == "running":
        return "wait"
    if attempt == "expired_unknown":
        _set(item, "unknown", now, "expired_unknown", preserve_inflight=True)
        return "review"
    if attempt in {"failed", "quota"}:
        _set(item, "review", now, attempt)
        return "review"
    if attempt == "source_changed":
        _set(item, "source_changed", now, "source_changed")
        return "source_changed"
    _set(item, "inflight", now, "attempt_unavailable", preserve_inflight=True)
    return "pause_unavailable"


def get_detail(api, item: dict, *, tries: int = 2) -> dict:
    last: Exception | None = None
    for _ in range(tries):
        try:
            return api.get(item)
        except ApiFailure as exc:
            if exc.status == 404:
                raise
            last = exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
    if isinstance(last, ApiFailure):
        raise last
    raise ApiFailure(503, "detail_transport")


def next_moscow_midnight(now: datetime) -> datetime:
    local = now.astimezone(MOSCOW)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)


def pause(state: dict, reason: str, until: datetime, now: datetime) -> None:
    state["pause_reason"] = reason
    state["pause_until"] = stamp(until)
    state["updated_at"] = stamp(now)


def clear_expired_pause(state: dict, now: datetime) -> None:
    value = state.get("pause_until")
    if value and parse_stamp(value) <= now:
        state["pause_until"] = None
        state["pause_reason"] = None
        state["updated_at"] = stamp(now)


def _reconcile_after_failure(api, item: dict, now: datetime) -> str:
    try:
        detail = get_detail(api, item)
    except ApiFailure:
        _set(item, "unknown", now, "post_reconcile_unavailable", preserve_inflight=True)
        return "unknown"
    action = apply_detail(item, detail, now, inflight_recovery=True)
    if action == "done":
        return "ready"
    attempt, valid = _attempt(detail)
    if valid and attempt in {"failed", "quota", "source_changed"}:
        if item["status"] not in {"review", "source_changed"}:
            _set(item, "review", now, attempt)
        return "closed"
    _set(item, "unknown", now, "post_outcome_unknown", preserve_inflight=True)
    return "unknown"


def _reconcile_unavailable(api, item: dict, now: datetime) -> str:
    """503 допускает повтор лишь когда GET доказал attempt.none."""
    try:
        detail = get_detail(api, item)
    except ApiFailure:
        _set(item, "unknown", now, "post_reconcile_unavailable", preserve_inflight=True)
        return "unknown"
    if detail.get("source_hash") == item["source_hash"] and detail.get("state") == "ready":
        _set(item, "ready", now, "ready")
        return "ready"
    attempt, valid = _attempt(detail)
    if (detail.get("source_hash") == item["source_hash"] and detail.get("state") == "available"
            and valid and attempt == "none"):
        _set(item, "pending", now, "translation_unavailable")
        return "safe"
    action = apply_detail(item, detail, now, inflight_recovery=True)
    if action in {"done", "source_changed", "review"}:
        return "closed"
    _set(item, "unknown", now, "post_outcome_unknown", preserve_inflight=True)
    return "unknown"


def process_one(api, item: dict, state: dict, now: datetime) -> tuple[str, bool]:
    """Возвращает (action, POST_started)."""
    try:
        detail = get_detail(api, item)
    except ApiFailure as exc:
        if exc.status == 404:
            _set(item, "gone", now, "carousel_not_found")
            return "continue", False
        item["code"] = "detail_unavailable"
        item["updated_at"] = stamp(now)
        pause(state, "detail_unavailable", now + timedelta(minutes=1), now)
        return "pause", False
    action = apply_detail(item, detail, now, inflight_recovery=False)
    if action != "post":
        state["consecutive_translation_failures"] = 0
        if action == "wait":
            pause(state, "attempt_running", now + timedelta(minutes=1), now)
            return "pause", False
        if item["status"] == "unknown":
            pause(state, "translation_outcome_unknown", now + timedelta(hours=1), now)
            return "pause", False
        if item["status"] == "review" and item["code"] == "quota":
            pause(state, "translation_quota_exceeded", next_moscow_midnight(now), now)
            return "pause", False
        if action.startswith("pause"):
            if action == "pause_old_api":
                pause(state, "old_api_contract", now + timedelta(seconds=60), now)
            else:
                pause(state, "translation_unavailable", now + timedelta(hours=1), now)
            return "pause", False
        return "continue", False
    try:
        api.post(item, period=detail.get("_period"))
    except (httpx.TransportError, httpx.TimeoutException):
        state["consecutive_translation_failures"] = 0
        _set(item, "unknown", now, "post_transport_unknown", preserve_inflight=True)
        return "pause", True
    except ApiFailure as exc:
        if not (exc.status == 502 and exc.code == "translation_failed"):
            state["consecutive_translation_failures"] = 0
        if exc.status == 409 and exc.code == "translation_review_required":
            _set(item, "review", now, exc.code)
            return "continue", True
        if exc.status == 409 and exc.code == "source_changed":
            _set(item, "source_changed", now, exc.code)
            return "continue", True
        if exc.status == 409 and exc.code == "translation_not_needed":
            _set(item, "not_needed", now, exc.code)
            return "continue", True
        if exc.status == 409 and exc.code == "translation_in_progress":
            item["code"] = exc.code
            item["updated_at"] = stamp(now)
            pause(state, "attempt_running", now + timedelta(minutes=1), now)
            return "pause", True
        if exc.status == 429 and exc.code == "translation_rate_limited":
            _set(item, "pending", now, exc.code)
            pause(state, exc.code, now + timedelta(seconds=exc.retry_after or 60), now)
            return "pause", True
        if exc.status == 429 and exc.code == "translation_quota_exceeded":
            _set(item, "review", now, exc.code)
            pause(state, exc.code, next_moscow_midnight(now), now)
            return "pause", True
        if exc.status == 502 and exc.code == "translation_failed":
            result = _reconcile_after_failure(api, item, now)
            if result == "ready":
                state["consecutive_translation_failures"] = 0
                return "continue", True
            if result == "closed":
                state["consecutive_translation_failures"] = min(3, state["consecutive_translation_failures"] + 1)
                if state["consecutive_translation_failures"] >= 3:
                    pause(state, "translation_failed_three", now + timedelta(minutes=15), now)
                    return "pause", True
                return "continue", True
            pause(state, "translation_outcome_unknown", now + timedelta(hours=1), now)
            return "pause", True
        if exc.status == 503:
            result = _reconcile_unavailable(api, item, now)
            if result == "ready":
                return "continue", True
            if result in {"safe", "closed"}:
                pause(state, "translation_unavailable", now + timedelta(hours=1), now)
            else:
                pause(state, "translation_outcome_unknown", now + timedelta(hours=1), now)
            return "pause", True
        if exc.status >= 500:
            _set(item, "unknown", now, "post_outcome_unknown", preserve_inflight=True)
            pause(state, "translation_outcome_unknown", now + timedelta(hours=1), now)
            return "pause", True
        if exc.status == 429:
            _set(item, "unknown", now, "post_outcome_unknown", preserve_inflight=True)
            pause(state, "translation_outcome_unknown", now + timedelta(hours=1), now)
            return "pause", True
        if exc.status in {401, 403}:
            result = _reconcile_after_failure(api, item, now)
            if result != "ready" and result != "closed":
                _set(item, "unknown", now, "translation_auth_failed", preserve_inflight=True)
            pause(state, "translation_auth_failed", now + timedelta(hours=1), now)
            return "pause", True
        if exc.status == 404:
            _set(item, "gone", now, "carousel_not_found")
            return "continue", True
        _set(item, "review", now, exc.code if re.fullmatch(r"[a-z0-9_]{1,64}", exc.code) else "post_error")
        return "continue", True
    _set(item, "ready", now, "ready")
    state["consecutive_translation_failures"] = 0
    return "continue", True


def _apply_unknown_detail(item: dict, detail: dict, now: datetime) -> str:
    digest, state = detail.get("source_hash"), detail.get("state")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        item["code"] = "old_api_contract"
        item["updated_at"] = stamp(now)
        return "pause"
    if digest != item["source_hash"]:
        _set(item, "source_changed", now, "source_changed")
        return "done"
    if state == "ready":
        _set(item, "ready", now, "ready")
        return "done"
    if state == "not_needed":
        _set(item, "not_needed", now, "translation_not_needed")
        return "done"
    if state == "unavailable":
        item["code"] = "translation_unavailable"
        item["updated_at"] = stamp(now)
        return "pause"
    if state != "available":
        item["code"] = "old_api_contract"
        item["updated_at"] = stamp(now)
        return "pause"
    attempt, valid = _attempt(detail)
    if not valid:
        item["code"] = "old_api_contract"
        item["updated_at"] = stamp(now)
        return "pause"
    if attempt in {"failed", "quota"}:
        _set(item, "review", now, attempt)
        return "done"
    if attempt == "source_changed":
        _set(item, "source_changed", now, "source_changed")
        return "done"
    item["code"] = attempt
    item["updated_at"] = stamp(now)
    return "pause" if attempt == "unavailable" else "wait"


def reconcile_inflight(api, state: dict, now: datetime) -> bool:
    paused = False
    quota_pause = False
    for item in state["items"]:
        original_status = item["status"]
        if original_status in {"review", "error", "source_changed"}:
            # Только GET: failed/unknown не возвращаются в pending даже при attempt.none.
            try:
                detail = get_detail(api, item)
            except ApiFailure:
                continue
            if detail.get("source_hash") == item["source_hash"]:
                if detail.get("state") == "ready":
                    _set(item, "ready", now, "ready")
                elif detail.get("state") == "not_needed":
                    _set(item, "not_needed", now, "translation_not_needed")
            continue
        if original_status not in {"inflight", "unknown"}:
            continue
        try:
            detail = get_detail(api, item)
            action = (_apply_unknown_detail(item, detail, now) if original_status == "unknown"
                      else apply_detail(item, detail, now, inflight_recovery=True))
        except ApiFailure as exc:
            if exc.status == 404:
                _set(item, "gone", now, "carousel_not_found")
                continue
            item["code"] = "detail_unavailable"
            item["updated_at"] = stamp(now)
            paused = True
            continue
        if action.startswith("pause") or action == "wait" or item["status"] == "unknown":
            paused = True
        if item["status"] == "review" and item["code"] == "quota":
            paused = True
            quota_pause = True
    if quota_pause:
        pause(state, "translation_quota_exceeded", next_moscow_midnight(now), now)
    elif paused:
        pause(state, "inflight_reconcile", now + timedelta(minutes=1), now)
    return paused


def counts(state: dict) -> dict[str, int]:
    return dict(Counter(item["status"] for item in state["items"]))


def _save(store, state: dict, sha: str | None) -> str:
    state["updated_at"] = stamp(utcnow())
    return store.save(state, sha)


def run(store, api, *, now_fn: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        max_seconds: int = MAX_RUN_SECONDS, max_posts: int = MAX_POSTS) -> tuple[str, dict[str, int]]:
    started = monotonic()
    now = now_fn()
    state, sha = store.load()
    clear_expired_pause(state, now)
    changed = discover(store, state, now)
    if changed:
        sha = _save(store, state, sha)
    reconcile_inflight(api, state, now)
    sha = _save(store, state, sha)
    pause_until = parse_stamp(state["pause_until"]) if state["pause_until"] else None
    if pause_until and pause_until > now:
        return "paused", counts(state)

    posts = 0
    work_deadline = max(0, max_seconds - REQUEST_DRAIN_SECONDS)
    while posts < max_posts and monotonic() - started < work_deadline:
        pending = [item for item in state["items"] if item["status"] == "pending"]
        if not pending:
            unresolved = {"inflight", "unknown", "error", "review", "source_changed"}
            status = "paused" if any(item["status"] in unresolved for item in state["items"]) else "completed"
            return status, counts(state)
        if store.remaining is not None and store.remaining < SAVE_RESERVE:
            pause(state, "github_rate_limit", store.reset_at or now + timedelta(hours=1), now)
            _save(store, state, sha)
            return "paused", counts(state)
        batch = pending[:BATCH_SIZE]
        batch_started = stamp(now_fn())
        for item in batch:
            item["status"] = "inflight"
            item["code"] = "checkpointed"
            item["updated_at"] = batch_started
            item["inflight_started_at"] = batch_started
        # Write-ahead CAS обязателен до первого POST в пачке.
        sha = _save(store, state, sha)
        stop = False
        for index, item in enumerate(batch):
            now = now_fn()
            if monotonic() - started >= work_deadline:
                for unsent in batch[index:]:
                    _set(unsent, "pending", now, "run_deadline")
                stop = True
                break
            action, posted = process_one(api, item, state, now)
            posts += int(posted)
            if action == "pause" or posts >= max_posts:
                # В этом процессе точно известно, что хвост пачки ещё не отправлялся.
                for unsent in batch[index + 1:]:
                    _set(unsent, "pending", now, "batch_unsent")
                stop = True
                break
        sha = _save(store, state, sha)
        if stop:
            return "paused" if state.get("pause_until") or any(i["status"] == "unknown" for i in batch) else "started", counts(state)
    return "started", counts(state)


def _summary(status: str, values: dict[str, int]) -> str:
    safe = " ".join(f"{name}={values[name]}" for name in sorted(values))
    return f"{status} {safe}".strip()


def _continuation_output(enabled: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        # Только штатный output Actions и фиксированная булева строка, без пользовательских данных.
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("needs_continuation=true\n" if enabled else "needs_continuation=false\n")


def main() -> int:
    _continuation_output(False)
    if os.environ.get("CAROUSEL_CAPTION_BULK_ENABLED", "").lower() != "true":
        print("paused code=disabled")
        return 0
    limit = os.environ.get("CAROUSEL_CAPTION_BULK_MAX_POSTS", "500")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", limit) or int(limit) > MAX_POSTS:
        print("paused code=invalid_max_posts")
        return 1
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("paused code=github_token_missing")
        return 1
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as github_client, \
                httpx.Client(follow_redirects=False, trust_env=False) as api_client:
            status, values = run(CarouselCaptionBulkStore(github_client, token), ProductionApi(api_client),
                                 max_posts=int(limit))
        if status == "started" and values.get("pending", 0) > 0:
            _continuation_output(True)
        print(_summary(status, values))
        return 0
    except Conflict:
        print("paused code=checkpoint_conflict")
        return 1
    except Exception as exc:  # noqa: BLE001 — граница CLI не печатает чувствительную ошибку.
        # Ни URL, ни тексты HTTP/подписей, ни credentials в stdout не попадают.
        print(f"paused code=worker_error type={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
