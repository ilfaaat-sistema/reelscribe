"""Облачная очередь обложек: только каталог и фиксированный Production API."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.services.carousel_cover_background_store import CarouselCoverBackgroundStore, Conflict, MODES, source_refs, upgrade_state

API_BASE = "https://reelscribe-ai.vercel.app/api"
PERIOD_PRIORITY = ("30d", "14d", "archive14d", "7d")
MAX_RUN_SECONDS = 80 * 60
REQUEST_DRAIN_SECONDS = 360
POST_ITEM_SECONDS = 300
POST_PERIOD_SECONDS = 150
BATCH_SIZE = 10
MAX_ACTIVE_POSTS = 2
MAX_POSTS = 2500
MOSCOW = ZoneInfo("Europe/Moscow")
_ID = re.compile(r"[0-9]{1,40}")
CACHE_STATUSES = {"missing", "running", "ready", "not_needed", "no_text", "unreadable", "image_unavailable", "error", "busy", "limit_reached", "disabled"}


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
        self.last_budget = {}

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

    def control(self):
        value = self._json(self.client.get(f"{API_BASE}/radar/carousel-cover-background/status", timeout=20))
        # Отсутствующая DDL/router или неизвестная версия безопасно останавливают новые пачки.
        return value.get("enabled") is True and value.get("mode") == "continuous"

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
            by_id[row["id"]] = {"status": row["status"], "retry_after": row.get("retry_after"),
                "reason_code": row.get("reason_code"), "attempt_recorded": row.get("attempt_recorded") is True}
        self.last_budget = value.get("budget") or {}
        return value["enabled"], by_id, self.last_budget.get("reset_at")

    def post(self, item):
        started = self.monotonic()
        requests = 0
        deadline = started + POST_ITEM_SECONDS
        try:
            for period in (p for p in PERIOD_PRIORITY if p in item["periods"]):
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise ApiFailure(504, "post_deadline")
                timeout = min(POST_PERIOD_SECONDS, remaining)
                requests += 1
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
                if type(value.get("attempt_reserved")) is not bool:
                    raise ApiFailure(502, "post_invalid_reservation")
                return {"status": row["status"], "retry_after": row.get("retry_after"),
                        "reason_code": row.get("reason_code"), "attempt_recorded": row.get("attempt_recorded") is True,
                        "attempt_reserved": value["attempt_reserved"], "http_requests": requests,
                        "http_ms": max(0, int((self.monotonic() - started) * 1000))}
            return {"status": "gone", "attempt_reserved": False, "http_requests": requests, "http_ms": max(0, int((self.monotonic() - started) * 1000))}
        except (ApiFailure, httpx.TransportError) as exc:
            exc.http_requests = requests
            exc.http_ms = max(0, int((self.monotonic() - started) * 1000))
            raise



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
    if status in {"ready", "not_needed", "no_text"}:
        set_item(item, status, now)
        return "done"
    if status in {"unreadable", "image_unavailable", "error"}:
        set_item(item, "review", now, row.get("reason_code") or status)
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
        set_item(item, "pending", now, "busy")
        return "wait"
    return "post"


def counts(state):
    items = [item for item in state["items"] if state["mode"] != "archive" or "archive14d" in item["periods"]]
    values = dict(Counter(item["status"] for item in items))
    values["remaining"] = sum(item["status"] in {"pending", "inflight", "unknown", "running"} for item in items)
    return values


def run(store, api, *, enabled=False, mode="existing", now_fn=utcnow, monotonic=time.monotonic,
        max_seconds=MAX_RUN_SECONDS, max_posts=MAX_POSTS, log=print, sleep_fn=time.sleep):
    if not enabled:
        return "disabled", {}
    if mode not in MODES:
        raise ValueError("Неизвестный режим")
    started = monotonic()
    deadline = max(0, max_seconds - REQUEST_DRAIN_SECONDS)
    state, sha = store.load()
    state = upgrade_state(state)
    reservations = 0
    progress = False
    busy_waits = 0

    def save():
        nonlocal sha
        state["updated_at"] = stamp(now_fn())
        sha = store.save(state, sha)
        remaining = store.remaining if store.remaining is not None else "unknown"
        reset = stamp(store.reset_at) if store.reset_at is not None else "unknown"
        log(f"checkpoint mode={mode} github_remaining={remaining} github_reset_at={reset} "
            + " ".join(f"{key}={value}" for key, value in sorted(counts(state).items())))

    def alive():
        return monotonic() - started < deadline

    def controlled():
        control = getattr(api, "control", None)
        if not callable(control):
            return True  # Локальные адаптеры тестов; ProductionApi всегда проверяет контроллер.
        try:
            if control():
                return True
            pause(state, "controller_disabled", now_fn())
        except (ApiFailure, httpx.TransportError):
            pause(state, "controller_unavailable", now_fn(), now_fn() + timedelta(minutes=5))
        return False

    def output(status):
        values = counts(state)
        # Самоцепочка нужна только при исчерпании времени с реальным прогрессом.
        values["needs_continuation"] = bool(progress and not alive() and not state["pause_reason"]
            and any(i["status"] == "pending" and (mode != "archive" or "archive14d" in i["periods"])
                    and (not i["defer_until"] or parse_stamp(i["defer_until"]) <= now_fn()) for i in state["items"]))
        return status, values

    now = now_fn()
    if state["pause_until"] and parse_stamp(state["pause_until"]) > now:
        return output("paused")
    state.update(pause_until=None, pause_reason=None)
    if not controlled():
        save()
        return output("paused")
    refs = None
    if mode == "continuous" and isinstance(store, CarouselCoverBackgroundStore):
        try:
            refs = source_refs(store)
        except (ValueError, RuntimeError, httpx.TransportError):
            pause(state, "source_refs_unavailable", now, now + timedelta(minutes=5))
            save()
            return output("paused")
    captured = state["inventory"] and parse_stamp(state["inventory"]["captured_at"]).astimezone(MOSCOW).date()
    if (state["inventory"] is None or state["mode"] != mode or
            mode == "continuous" and (refs is None or refs != state["source_refs"] or captured != now.astimezone(MOSCOW).date())):
        try:
            rows = api.inventory(mode, can_continue=alive)
        except (ApiFailure, httpx.TransportError) as exc:
            pause(state, "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "inventory_unavailable", now)
            save()
            return output("paused")
        merge_inventory(state, rows, mode, now)
        if refs is not None:
            state["source_refs"] = refs
        save()
    state["mode"] = mode

    def refresh(batch):
        nonlocal progress
        api_enabled, rows, reset = api.read([item["id"] for item in batch])
        if not api_enabled:
            pause(state, "api_disabled", now_fn())
            return []
        eligible = []
        for item in batch:
            old = item["status"]
            action = apply_cache(item, rows[item["id"]], state, now_fn(), reset)
            if action == "done" and old != item["status"]:
                state["counters"]["cache_hits"] += 1
                progress = True
            if action == "post" and (not item["defer_until"] or parse_stamp(item["defer_until"]) <= now_fn()):
                eligible.append(item)
        budget = getattr(api, "last_budget", {})
        if budget.get("status") == "limit_reached":
            budget_pause(state, {}, budget.get("reset_at"), now_fn())
        return eligible

    candidates = [item for item in state["items"] if item["status"] in {"pending", "inflight", "running", "unknown"}
                  and (mode != "archive" or "archive14d" in item["periods"])]
    eligible = []
    for start in range(0, len(candidates), 100):
        if not alive():
            save()
            return output("started")
        try:
            eligible.extend(refresh(candidates[start:start + 100]))
        except (ApiFailure, httpx.TransportError) as exc:
            pause(state, "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "cache_unavailable", now_fn())
        save()
        if state["pause_reason"]:
            return output("paused")

    while eligible and reservations < max_posts and alive():
        if not controlled():
            save()
            return output("paused")
        if store.remaining is not None and store.remaining < 20:
            pause(state, "github_rate_limit", now_fn(), store.reset_at or now_fn() + timedelta(hours=1))
            save()
            return output("paused")
        capacity = getattr(api, "last_budget", {}).get("available_slots", 2)
        busy = capacity == 0
        if not busy:
            batch = eligible[:BATCH_SIZE]
            eligible = eligible[len(batch):]
            previous_first = {item["id"]: item["first_inflight_at"] for item in batch}
            for item in batch:
                set_item(item, "inflight", now_fn(), "checkpointed")
                item["first_inflight_at"] = item["first_inflight_at"] or stamp(now_fn())
            # Один CAS до группы: после crash даже неотправленный хвост требует GET-сверки.
            save()
            cursor = 0
            with ThreadPoolExecutor(max_workers=MAX_ACTIVE_POSTS) as pool:
                while cursor < len(batch) and reservations < max_posts and alive():
                    if busy or state["pause_reason"] or not controlled():
                        break
                    capacity = getattr(api, "last_budget", {}).get("available_slots", MAX_ACTIVE_POSTS)
                    if capacity <= 0:
                        busy = True
                        break
                    size = min(MAX_ACTIVE_POSTS, capacity, max_posts - reservations, len(batch) - cursor)
                    wave = batch[cursor:cursor + size]
                    cursor += len(wave)
                    # Следующая пара не попадает даже в executor, пока эта волна не завершена.
                    futures = {pool.submit(api.post, item): (item, time.monotonic()) for item in wave}
                    for future, (item, request_started) in futures.items():
                        try:
                            result = future.result()
                            item["http_ms"] = result["http_ms"]
                            status = result["status"]
                            if status in {"missing", "disabled"}:
                                raise ApiFailure(502, "post_invalid_result")
                            requests = result.get("http_requests", 1)
                            state["counters"]["posts"] += requests
                            state["counters"]["http_requests"] += requests
                            if result.get("attempt_reserved") is True:
                                reservations += 1
                                state["counters"]["attempts_reserved"] += 1
                            elif status in {"ready", "not_needed", "no_text", "unreadable", "error"}:
                                state["counters"]["cache_hits"] += 1
                            if status == "gone":
                                set_item(item, "gone", now_fn(), "carousel_not_found")
                                progress = True
                            elif status in {"busy", "limit_reached"}:
                                # Явный отказ до reserve; намерение можно безопасно вернуть.
                                set_item(item, "pending", now_fn(), status)
                                item["first_inflight_at"] = None
                                item["defer_until"] = None
                                if status == "busy":
                                    state["counters"]["busy_rejections"] += 1
                                    busy = True
                                    eligible.append(item)
                                else:
                                    budget_pause(state, result, None, now_fn())
                            else:
                                action = apply_cache(item, result, state, now_fn())
                                progress = progress or action == "done"
                                if status == "error":
                                    state["consecutive_errors"] = min(3, state["consecutive_errors"] + 1)
                                    state["counters"]["errors"] += 1
                                else:
                                    state["consecutive_errors"] = 0
                                    state["counters"]["ready" if status == "ready" else "skipped"] += 1
                            state["counters"]["http_ms"] += result["http_ms"]
                            if result.get("reason_code") == "provider_rate_limit":
                                until = now_fn() + timedelta(seconds=60)
                                try:
                                    until = max(now_fn() + timedelta(seconds=1), min(
                                        parse_stamp(result.get("retry_after") or ""), now_fn() + timedelta(minutes=5)))
                                except (ValueError, AttributeError):
                                    pass
                                pause(state, "provider_rate_limit", now_fn(), until)
                            if state["consecutive_errors"] >= 3:
                                pause(state, "three_errors", now_fn(), now_fn() + timedelta(minutes=15))
                        except Exception as exc:  # noqa: BLE001 — потерянный ответ не разрешает повтор.
                            requests = getattr(exc, "http_requests", 1)
                            item["http_ms"] = getattr(exc, "http_ms", max(0, int((time.monotonic() - request_started) * 1000)))
                            state["counters"]["http_ms"] += item["http_ms"]
                            state["counters"]["posts"] += requests
                            state["counters"]["http_requests"] += requests
                            code = "auth_failed" if isinstance(exc, ApiFailure) and exc.status in {401, 403} else "post_outcome_unknown"
                            set_item(item, "unknown", now_fn(), code)
                            state["counters"]["errors"] += 1
                            pause(state, code, now_fn())
                        log(f"cover id={item['id']} status={item['status']} http_ms={item['http_ms']} mode={mode}")
            # Только живой процесс знает, что хвост не отправлялся; после crash такого права нет.
            unsent = batch[cursor:]
            for item in unsent:
                set_item(item, "pending", now_fn(), "batch_unsent")
                item["first_inflight_at"] = previous_first[item["id"]]
            eligible = unsent + eligible
            # Второй CAS сохраняет все волны группы и возвращённый хвост одним результатом.
            save()
        if state["pause_reason"]:
            return output("paused")
        if busy:
            # Ни один новый ID не выдаётся, пока GET не подтвердит свободную ёмкость.
            recovered = False
            for delay in (5, 10, 20)[busy_waits:]:
                if monotonic() - started + delay >= deadline:
                    break
                sleep_fn(delay)
                busy_waits += 1
                if not controlled():
                    break
                try:
                    eligible = refresh(eligible[:100]) + eligible[100:]
                    recovered = getattr(api, "last_budget", {}).get("available_slots", 0) > 0
                except (ApiFailure, httpx.TransportError):
                    recovered = False
                save()
                if recovered or state["pause_reason"]:
                    break
            if state["pause_reason"]:
                return output("paused")
            if not recovered:
                pause(state, "busy_backpressure", now_fn(), now_fn() + timedelta(minutes=5))
                save()
                return output("paused")
        else:
            busy_waits = 0
    # Все изменения очереди уже записаны initial GET или вторым CAS группы.
    if not candidates:
        save()
    return output("completed" if counts(state)["remaining"] == 0 else "started")


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
            status, values = run(CarouselCoverBackgroundStore(github_client, token, request_timeout=15), ProductionApi(api_client), enabled=True, mode=mode)
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            with open(output_path, "a", encoding="utf-8") as output:
                output.write(f"needs_continuation={'true' if values.get('needs_continuation') else 'false'}\n")
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
