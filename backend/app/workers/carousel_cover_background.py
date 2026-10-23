"""Облачная очередь обложек: только каталог и фиксированный Production API."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.services.carousel_cover_background_store import CarouselCoverBackgroundStore, Conflict, MODES

API_BASE = "https://reelscribe-ai.vercel.app/api"
PERIOD_PRIORITY = ("30d", "14d", "archive14d", "7d")
MAX_RUN_SECONDS = 1200
REQUEST_DRAIN_SECONDS = 360
POST_ITEM_SECONDS = 300
POST_PERIOD_SECONDS = 150
BATCH_SIZE = 10
MAX_POSTS = 500
MOSCOW = ZoneInfo("Europe/Moscow")
_ID = re.compile(r"[0-9]{1,40}")
CACHE_STATUSES = {"missing", "running", "ready", "no_text", "unreadable", "image_unavailable", "error", "busy", "limit_reached", "disabled"}


class ApiFailure(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def parse_stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Дата без часового пояса")
    return result.astimezone(timezone.utc)


def next_midnight(now):
    return (now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)


class ProductionApi:
    def __init__(self, client, base=API_BASE, *, monotonic=None):
        if base != API_BASE:
            raise ValueError("Разрешён только Production API")
        self.client = client
        self.monotonic = monotonic or time.monotonic

    @staticmethod
    def _json(response):
        if response.status_code != 200:
            raise ApiFailure(response.status_code, "api_http_error")
        if len(response.content) > 4 * 1024 * 1024:
            raise ApiFailure(503, "api_response_too_large")
        try:
            value = response.json()
        except ValueError:
            raise ApiFailure(503, "api_invalid_json") from None
        if not isinstance(value, dict):
            raise ApiFailure(503, "api_invalid_json")
        return value

    def inventory(self, mode, *, can_continue=lambda: True):
        found = {}
        for period in (("archive14d",) if mode == "archive" else ("archive14d", "7d", "14d", "30d")):
            offset = 0
            while True:
                if not can_continue():
                    raise ApiFailure(503, "inventory_deadline")
                value = self._json(self.client.get(f"{API_BASE}/radar/carousels", params={
                    "period": period, "limit": 100, "offset": offset, "favorites_only": "false",
                    "sort": "timestamp", "order": "asc",
                }, timeout=45))
                rows, total = value.get("items"), value.get("total")
                if not isinstance(rows, list) or len(rows) > 100 or type(total) is not int or not 0 <= total <= 100_000:
                    raise ApiFailure(503, "inventory_invalid")
                for row in rows:
                    identity = str(row.get("id", "")) if isinstance(row, dict) else ""
                    if not _ID.fullmatch(identity):
                        raise ApiFailure(503, "inventory_invalid_id")
                    found.setdefault(identity, set()).add(period)
                offset += len(rows)
                if offset >= total:
                    break
                if not rows:
                    raise ApiFailure(503, "inventory_incomplete")
                if offset >= 100_000:
                    raise ApiFailure(503, "inventory_too_large")
        return [{"id": identity, "periods": [p for p in PERIOD_PRIORITY if p in periods]}
                for identity, periods in sorted(found.items())]

    def read(self, ids):
        value = self._json(self.client.get(f"{API_BASE}/radar/carousel-cover-translations",
            params={"ids": ",".join(ids)}, timeout=45))
        rows = value.get("items")
        if type(value.get("enabled")) is not bool or not isinstance(rows, list) or len(rows) != len(ids):
            raise ApiFailure(503, "cache_invalid")
        by_id = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("id") not in ids or row["id"] in by_id or row.get("status") not in CACHE_STATUSES:
                raise ApiFailure(503, "cache_invalid")
            # Тексты не переходят в очередь, checkpoint или журналы.
            by_id[row["id"]] = {"status": row["status"], "retry_after": row.get("retry_after")}
        return value["enabled"], by_id, (value.get("budget") or {}).get("reset_at")

    def post(self, item):
        started = self.monotonic()
        deadline = started + POST_ITEM_SECONDS
        for period in (p for p in PERIOD_PRIORITY if p in item["periods"]):
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ApiFailure(504, "post_deadline")
            timeout = min(POST_PERIOD_SECONDS, remaining)
            response = self.client.post(f"{API_BASE}/radar/carousel-cover-translations/{item['id']}",
                json={"period": period}, timeout=httpx.Timeout(
                    timeout, connect=min(10, timeout), write=min(10, timeout), pool=min(5, timeout),
                ))
            if self.monotonic() >= deadline:
                raise ApiFailure(504, "post_deadline")
            # Эти ответы сервер отдаёт до reserve/model; другой сохранённый период безопасен.
            if response.status_code in {404, 422}:
                continue
            value = self._json(response)
            row = value.get("item")
            if not isinstance(row, dict) or row.get("id") != item["id"] or row.get("status") not in CACHE_STATUSES:
                raise ApiFailure(502, "post_invalid_result")
            return {"status": row["status"], "retry_after": row.get("retry_after"),
                    "http_ms": max(0, int((self.monotonic() - started) * 1000))}
        return {"status": "gone", "http_ms": max(0, int((self.monotonic() - started) * 1000))}


def set_item(item, status, now, code=None):
    item.update(status=status, updated_at=stamp(now), safe_error_code=code)


def pause(state, reason, now, until=None):
    state.update(pause_reason=reason, pause_until=stamp(until) if until else None, updated_at=stamp(now))


def budget_pause(state, row, reset, now):
    # Даже неполный ответ не превращает дневной лимит в быстрые POST-повторы.
    until = next_midnight(now)
    for raw in (row.get("retry_after"), reset):
        if isinstance(raw, str):
            try:
                until = max(until, parse_stamp(raw))
            except ValueError:
                pass
    pause(state, "limit_reached", now, until)


def merge_inventory(state, rows, mode, now):
    previous = {item["id"]: item for item in state["items"]}
    for row in rows:
        if row["id"] in previous:
            item = previous[row["id"]]
            item["periods"] = [p for p in PERIOD_PRIORITY if p in set(item["periods"] + row["periods"])]
        else:
            previous[row["id"]] = {**row, "status": "pending", "updated_at": stamp(now),
                "first_inflight_at": None, "safe_error_code": None, "http_ms": 0, "defer_until": None}
    state["items"] = sorted(previous.values(), key=lambda item: item["id"])
    state["mode"] = mode
    identity = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    state["inventory"] = {"sha256": hashlib.sha256(identity).hexdigest(), "count": len(rows),
                          "captured_at": stamp(now), "scope": mode}


def apply_cache(item, row, state, now, reset=None):
    status = row["status"]
    previous = item["status"]
    if status in {"ready", "no_text"}:
        set_item(item, status, now)
        return "done"
    if status in {"unreadable", "image_unavailable", "error"}:
        set_item(item, "review", now, status)
        return "done"
    if status == "limit_reached":
        budget_pause(state, row, reset, now)
        return "stop"
    if status == "disabled":
        pause(state, "api_disabled", now)
        return "stop"
    if status == "running":
        if previous != "unknown":
            set_item(item, "running", now, "server_running")
        return "wait"
    if previous in {"inflight", "unknown", "running"}:
        # После crash нет доказательства, что намерение не успело стать платным вызовом.
        if not item["first_inflight_at"]:
            item["first_inflight_at"] = stamp(now)
        set_item(item, "unknown", now, "recovery_missing")
        return "wait"
    if status == "busy":
        item["defer_until"] = stamp(now + timedelta(minutes=15))
        set_item(item, "pending", now, "busy")
        return "wait"
    return "post"


def counts(state):
    items = [item for item in state["items"] if state["mode"] != "archive" or "archive14d" in item["periods"]]
    values = dict(Counter(item["status"] for item in items))
    values["remaining"] = sum(item["status"] in {"pending", "inflight", "unknown", "running"} for item in items)
    return values


def run(store, api, *, enabled=False, mode="existing", now_fn=utcnow, monotonic=time.monotonic,
        max_seconds=MAX_RUN_SECONDS, max_posts=MAX_POSTS, log=print):
    if not enabled:
        return "disabled", {}
    if mode not in MODES:
        raise ValueError("Неизвестный режим")
    started = monotonic()
    deadline = max(0, max_seconds - REQUEST_DRAIN_SECONDS)
    state, sha = store.load()

    def save():
        nonlocal sha
        state["updated_at"] = stamp(now_fn())
        sha = store.save(state, sha)

    now = now_fn()
    # Активная бюджетная пауза не держит runner и не требует сети Production.
    if state["pause_until"] and parse_stamp(state["pause_until"]) > now:
        return "paused", counts(state)
    state.update(pause_until=None, pause_reason=None)
    if state["inventory"] is None or mode == "continuous" or state["mode"] != mode:
        try:
            rows = api.inventory(mode, can_continue=lambda: monotonic() - started < deadline)
        except (ApiFailure, httpx.TransportError) as exc:
            pause(state, "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "inventory_unavailable", now)
            save()
            return "paused", counts(state)
        merge_inventory(state, rows, mode, now)
        save()
    state["mode"] = mode
    # Сначала только GET: потерянные результаты никогда не дают права на повтор POST.
    candidates = [item for item in state["items"] if item["status"] in {"pending", "inflight", "running", "unknown"}
                  and (mode != "archive" or "archive14d" in item["periods"])]
    eligible = []
    for start in range(0, len(candidates), 100):
        if monotonic() - started >= deadline:
            save()
            return "started", counts(state)
        batch = candidates[start:start + 100]
        try:
            api_enabled, rows, reset = api.read([item["id"] for item in batch])
        except (ApiFailure, httpx.TransportError) as exc:
            pause(state, "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "cache_unavailable", now_fn())
            save()
            return "paused", counts(state)
        if not api_enabled:
            pause(state, "api_disabled", now_fn())
            save()
            return "paused", counts(state)
        for item in batch:
            action = apply_cache(item, rows[item["id"]], state, now_fn(), reset)
            if action == "post" and (not item["defer_until"] or parse_stamp(item["defer_until"]) <= now_fn()):
                eligible.append(item)
        save()
        if state["pause_reason"]:
            return "paused", counts(state)
    posts = 0
    while eligible and posts < max_posts and monotonic() - started < deadline:
        if store.remaining is not None and store.remaining < 20:
            pause(state, "github_rate_limit", now_fn(), store.reset_at or now_fn() + timedelta(hours=1))
            save()
            return "paused", counts(state)
        batch = eligible[:min(BATCH_SIZE, max_posts - posts)]
        eligible = eligible[len(batch):]
        for item in batch:
            set_item(item, "inflight", now_fn(), "checkpointed")
            item["first_inflight_at"] = item["first_inflight_at"] or stamp(now_fn())
        # CAS намерения проверяется до любого платного запроса пачки.
        save()
        stop = False
        cursor = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            while futures or (cursor < len(batch) and not stop):
                while not stop and cursor < len(batch) and len(futures) < 2:
                    if monotonic() - started >= deadline:
                        stop = True
                        break
                    item = batch[cursor]
                    cursor += 1
                    futures[pool.submit(api.post, item)] = (item, time.monotonic())
                    posts += 1
                    state["counters"]["posts"] += 1
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    item, request_started = futures.pop(future)
                    try:
                        result = future.result()
                        item["http_ms"] = result["http_ms"]
                        status = result["status"]
                        if status in {"missing", "disabled"}:
                            raise ApiFailure(502, "post_invalid_result")
                        if status == "gone":
                            set_item(item, "gone", now_fn(), "carousel_not_found")
                        elif status in {"busy", "limit_reached"}:
                            # Явный отказ до reserve: этот процесс знает, что попытка не началась.
                            set_item(item, "pending", now_fn(), status)
                            item["defer_until"] = stamp(now_fn() + timedelta(minutes=15)) if status == "busy" else None
                            if status == "limit_reached":
                                budget_pause(state, result, None, now_fn())
                                stop = True
                        else:
                            apply_cache(item, result, state, now_fn())
                            if status == "error":
                                state["consecutive_errors"] = min(3, state["consecutive_errors"] + 1)
                                state["counters"]["errors"] += 1
                            else:
                                state["consecutive_errors"] = 0
                                state["counters"]["ready" if status == "ready" else "skipped"] += 1
                        state["counters"]["http_ms"] += result["http_ms"]
                        if state["consecutive_errors"] >= 3:
                            pause(state, "three_errors", now_fn(), now_fn() + timedelta(minutes=15))
                            stop = True
                    except Exception as exc:  # noqa: BLE001 — неизвестный результат требует только GET.
                        item["http_ms"] = max(0, int((time.monotonic() - request_started) * 1000))
                        state["counters"]["http_ms"] += item["http_ms"]
                        code = "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "post_outcome_unknown"
                        set_item(item, "unknown", now_fn(), code)
                        state["counters"]["errors"] += 1
                        pause(state, code, now_fn())
                        stop = True
                    log(f"cover id={item['id']} status={item['status']} http_ms={item['http_ms']} mode={mode}")
        # Только хвост текущего живого процесса достоверно не отправлялся.
        for item in batch[cursor:]:
            set_item(item, "pending", now_fn(), "batch_unsent")
        save()
        log(f"checkpoint mode={mode} pause={state['pause_reason'] or 'none'} " + " ".join(f"{k}={v}" for k, v in sorted(counts(state).items())))
        if stop:
            return "paused" if state["pause_reason"] else "started", counts(state)
    values = counts(state)
    return ("completed" if values["remaining"] == 0 else "started"), values


def main():
    if os.environ.get("CAROUSEL_COVER_BACKGROUND_ENABLED", "").lower() != "true":
        print("paused code=disabled")
        return 0
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("paused code=github_token_missing")
        return 1
    mode = os.environ.get("CAROUSEL_COVER_BACKGROUND_MODE", "existing")
    try:
        # HTTP keep-alive, никаких транспортных повторов POST и редиректов.
        with httpx.Client(follow_redirects=False, trust_env=False) as github_client, \
                httpx.Client(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=2)) as api_client:
            status, values = run(CarouselCoverBackgroundStore(github_client, token), ProductionApi(api_client), enabled=True, mode=mode)
        print(f"{status} mode={mode} " + " ".join(f"{k}={v}" for k, v in sorted(values.items())))
        return 0
    except Conflict:
        print("paused code=checkpoint_conflict")
        return 1
    except Exception as exc:  # noqa: BLE001 — не печатаем HTTP-тексты и реквизиты.
        print(f"paused code=worker_error type={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
