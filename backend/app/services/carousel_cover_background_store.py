"""CAS-хранилище очереди фонового перевода обложек в отдельной ветке GitHub."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import re
from datetime import datetime, timezone

REPOSITORY = "ilfaaat-sistema/reelscribe"
BRANCH = "data/carousel-cover-background"
MAX_BYTES = 10 * 1024 * 1024
MARKER = "Данные очереди перевода обложек каруселей; код приложения отсутствует.\n"
FILES = {"state.json", "vercel.json", ".carousel-cover-background-data"}
PERIODS = {"7d", "14d", "30d", "archive14d"}
MODES = {"existing", "continuous", "archive"}
STATUSES = {"pending", "inflight", "running", "ready", "not_needed", "no_text", "review", "unknown", "gone"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ID = re.compile(r"[0-9]{1,40}")
_SAFE_CODE = re.compile(r"[a-z0-9_]{1,64}")
_SECRET = re.compile(
    r"github_pat_[A-Za-z0-9_]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|EAA[A-Za-z0-9]{40,}|"
    r'"(?:caption|translation|token|authorization|api_key|service_role_key|decrypted_secret)"\s*:',
    re.IGNORECASE,
)
_BASE64_ASCII_WHITESPACE = re.compile(r"[ \t\r\n\f\v]")


class Conflict(RuntimeError):
    """Ветка изменилась после чтения; автоматическое слияние для inflight небезопасно."""


def _decode_base64(content: object, error: str) -> bytes:
    if not isinstance(content, str):
        raise ValueError(error)  # noqa: TRY004 — единая ошибка повреждённых внешних данных.
    # GitHub переносит base64 по строкам. Удаляем только ASCII whitespace;
    # validate=True по-прежнему отклоняет любые остальные посторонние символы.
    compact = _BASE64_ASCII_WHITESPACE.sub("", content)
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, TypeError):
        raise ValueError(error) from None


def _iso(value: object, *, nullable: bool = True) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Некорректная дата очереди")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Некорректная дата очереди") from None
    if result.tzinfo is None:
        raise ValueError("Дата очереди должна иметь часовой пояс")
    return result.astimezone(timezone.utc)


def initial_state(mode: str = "existing") -> dict:
    return {
        "version": 2, "mode": mode, "updated_at": datetime.now(timezone.utc).isoformat(),
        "inventory": None, "items": [], "pause_until": None, "pause_reason": None,
        "consecutive_errors": 0, "source_refs": {}, "recovery_events": [],
        "counters": {"posts": 0, "ready": 0, "skipped": 0, "errors": 0, "http_ms": 0,
                     "http_requests": 0, "attempts_reserved": 0, "cache_hits": 0, "busy_rejections": 0},
    }


def upgrade_state(state: dict) -> dict:
    """v1.posts — исторический HTTP-счётчик; оплаченные попытки из него не выводим."""
    validate_state(state)
    value = deepcopy(state)
    if value["version"] == 1:
        value.update(version=2, source_refs={}, recovery_events=[])
        value["counters"].update(http_requests=value["counters"]["posts"], attempts_reserved=0,
                                 cache_hits=0, busy_rejections=0)
    return value


def reconcile_queue(store, manifest: dict, *, now: datetime) -> str:
    """Чистый операторский CAS: только явно сверенные результаты, никаких HTTP POST."""
    _iso(now.isoformat(), nullable=False)
    if not isinstance(manifest, dict) or set(manifest) != {"expected_sha", "items"}:
        raise ValueError("Некорректный manifest восстановления")
    if not _GIT_SHA.fullmatch(str(manifest["expected_sha"])) or not isinstance(manifest["items"], list) or not manifest["items"]:
        raise ValueError("Некорректный manifest восстановления")
    state, sha = store.load()
    if sha != manifest["expected_sha"]:
        raise Conflict("Очередь изменилась после операторской сверки")
    state = upgrade_state(state)
    by_id = {item["id"]: item for item in state["items"]}
    seen = set()
    common = {"id", "expected_status", "expected_updated_at", "expected_first_inflight_at", "action"}
    fields = {
        "verified_no_attempt": {"db_all_versions_checked", "db_claim_absent", "old_run_finished"},
        "db_rearmed": {"db_rearmed"},
        "resolved_without_model": {"resolved_without_model", "observed_status"},
    }
    for row in manifest["items"]:
        if not isinstance(row, dict) or row.get("action") not in fields or set(row) != common | fields[row["action"]]:
            raise ValueError("Некорректная сверка восстановления")
        if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]) or row["id"] in seen:
            raise ValueError("Некорректный или повторный ID восстановления")
        seen.add(row["id"])
        item = by_id.get(row["id"])
        if (not item or item["status"] != row["expected_status"] or item["updated_at"] != row["expected_updated_at"]
                or item["first_inflight_at"] != row["expected_first_inflight_at"]):
            raise Conflict("Запись очереди изменилась после операторской сверки")
        action = row["action"]
        target = "pending"
        if action == "verified_no_attempt":
            first = _iso(item["first_inflight_at"])
            if (item["status"] != "unknown" or first is None or (now - first).total_seconds() <= 600
                    or any(row[key] is not True for key in fields[action])):
                raise ValueError("Нужны сверка всех DB-версий, отсутствие claim, завершённый run и возраст более600с")
            reason = "operator_confirmed_no_claim"
        elif action == "db_rearmed":
            if item["status"] != "review" or row["db_rearmed"] is not True:
                raise ValueError("Нужен успешный DB rearm для review")
            reason = "operator_db_rearmed"
        else:
            if (item["status"] not in {"pending", "review"} or row["resolved_without_model"] is not True
                    or row["observed_status"] not in {"not_needed", "no_text"}):
                raise ValueError("Нужен терминальный GET-результат без модели")
            target = row["observed_status"]
            reason = "operator_resolved_without_model"
        state["recovery_events"].append({"id": item["id"], "at": now.isoformat(),
            "previous_updated_at": item["updated_at"], "first_inflight_at": item["first_inflight_at"],
            "previous_status": item["status"], "reason": reason})
        item.update(status=target, updated_at=now.isoformat(), first_inflight_at=None,
                    safe_error_code=reason, defer_until=None)
    state["updated_at"] = now.isoformat()
    return store.save(state, sha)


def reconcile_unknown(store, manifest: dict, *, now: datetime) -> str:
    """Совместимый узкий helper; manifest обязательно подтверждает все версии БД."""
    if not isinstance(manifest, dict) or set(manifest) != {"expected_sha", "items"} or not isinstance(manifest["items"], list):
        raise ValueError("Некорректный manifest неизвестных исходов")
    converted = {"expected_sha": manifest["expected_sha"], "items": []}
    for row in manifest["items"]:
        if not isinstance(row, dict) or set(row) != {"id", "expected_updated_at", "expected_first_inflight_at",
                "db_claim_absent", "db_all_versions_checked", "old_run_finished"}:
            raise ValueError("Нужна явная сверка всех DB-версий неизвестного исхода")
        converted["items"].append({**row, "expected_status": "unknown", "action": "verified_no_attempt"})
    return reconcile_queue(store, converted, now=now)


def validate_state(state: object) -> str:
    if not isinstance(state, dict):
        raise ValueError("Некорректная схема очереди")
    version = state.get("version")
    extra = {"source_refs", "recovery_events"} if version == 2 else set()
    if set(state) != {
        "version", "mode", "updated_at", "inventory", "items", "pause_until", "pause_reason",
        "consecutive_errors", "counters",
    } | extra or version not in (1, 2) or state.get("mode") not in MODES:
        raise ValueError("Некорректная схема очереди")
    _iso(state["updated_at"], nullable=False)
    _iso(state["pause_until"])
    if state["pause_reason"] is not None and (not isinstance(state["pause_reason"], str) or not _SAFE_CODE.fullmatch(state["pause_reason"])):
        raise ValueError("Некорректная причина паузы")
    if type(state["consecutive_errors"]) is not int or not 0 <= state["consecutive_errors"] <= 3:
        raise ValueError("Некорректный счётчик ошибок")
    inventory = state["inventory"]
    if inventory is not None:
        if not isinstance(inventory, dict) or set(inventory) != {"sha256", "count", "captured_at", "scope"}:
            raise ValueError("Некорректная инвентаризация")
        if not isinstance(inventory["sha256"], str) or not _HEX64.fullmatch(inventory["sha256"]):
            raise ValueError("Некорректный hash инвентаризации")
        if type(inventory["count"]) is not int or inventory["count"] < 0 or inventory["scope"] not in MODES:
            raise ValueError("Некорректный объём инвентаризации")
        _iso(inventory["captured_at"], nullable=False)
    if version == 2:
        refs = state["source_refs"]
        if not isinstance(refs, dict) or any(name not in {"archive7d", "archive14d", "live"} or not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha) for name, sha in refs.items()):
            raise ValueError("Некорректные SHA источников")
        if not isinstance(state["recovery_events"], list) or len(state["recovery_events"]) > 100_000:
            raise ValueError("Некорректная история восстановления")
        for event in state["recovery_events"]:
            if (not isinstance(event, dict) or set(event) not in ({"id", "at", "previous_updated_at", "first_inflight_at", "reason"},
                                             {"id", "at", "previous_updated_at", "first_inflight_at", "previous_status", "reason"})
                    or not isinstance(event["id"], str) or not _ID.fullmatch(event["id"])
                    or event["reason"] not in {"operator_confirmed_no_claim", "operator_db_rearmed", "operator_resolved_without_model"}
                    or event.get("previous_status", "unknown") not in STATUSES):
                raise ValueError("Некорректное событие восстановления")
            for field in ("at", "previous_updated_at"):
                _iso(event[field], nullable=False)
            _iso(event["first_inflight_at"])
    counters = state["counters"]
    counter_extra = {"http_requests", "attempts_reserved", "cache_hits", "busy_rejections"} if version == 2 else set()
    if not isinstance(counters, dict) or set(counters) != {"posts", "ready", "skipped", "errors", "http_ms"} | counter_extra or any(type(v) is not int or v < 0 for v in counters.values()):
        raise ValueError("Некорректные счётчики")
    items = state["items"]
    if not isinstance(items, list) or len(items) > 100_000:
        raise ValueError("Некорректный размер очереди")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "periods", "status", "updated_at", "first_inflight_at", "safe_error_code", "http_ms", "defer_until"}:
            raise ValueError("Некорректная запись очереди")
        identity = item["id"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in seen:
            raise ValueError("Некорректный или повторный ID")
        seen.add(identity)
        periods = item["periods"]
        if not isinstance(periods, list) or not periods or any(not isinstance(p, str) or p not in PERIODS for p in periods) or len(periods) != len(set(periods)):
            raise ValueError("Некорректные периоды")
        if item["status"] not in STATUSES:
            raise ValueError("Некорректный статус")
        _iso(item["updated_at"], nullable=False)
        first = _iso(item["first_inflight_at"])
        _iso(item["defer_until"])
        if item["status"] in {"inflight", "unknown"} and first is None:
            raise ValueError("Неизвестная попытка без времени")
        code = item["safe_error_code"]
        if code is not None and (not isinstance(code, str) or not _SAFE_CODE.fullmatch(code)):
            raise ValueError("Некорректный код")
        if type(item["http_ms"]) is not int or item["http_ms"] < 0:
            raise ValueError("Некорректная длительность HTTP")
    text = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    scan = {**state, "inventory": None}
    if _SECRET.search(json.dumps(scan, ensure_ascii=False)):
        raise ValueError("В очереди обнаружены запрещённые данные")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Превышен размер очереди")
    return text


class CarouselCoverBackgroundStore:
    def __init__(self, client, token: str | None = None, *, request_timeout=45):
        self.client = client
        self.token = token
        self.request_timeout = request_timeout
        self.base = f"https://api.github.com/repos/{REPOSITORY}/"
        self.remaining: int | None = None
        self.reset_at: datetime | None = None

    def request(self, method: str, path: str, *, missing: bool = False, conflict: bool = False,
                accept: str = "application/vnd.github+json", **kwargs):
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.client.request(method, self.base + path, headers=headers, timeout=self.request_timeout, **kwargs)
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                self.remaining = max(0, int(remaining))
            except ValueError:
                self.remaining = 0
        elif self.remaining is not None:
            self.remaining = max(0, self.remaining - 1)
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                self.reset_at = datetime.fromtimestamp(int(reset), timezone.utc)
            except (ValueError, OSError):
                self.reset_at = None
        if missing and response.status_code == 404:
            return None
        if conflict and response.status_code in (409, 422):
            raise Conflict("Контрольная точка изменилась параллельно")
        if not 200 <= response.status_code < 300:
            raise RuntimeError("Хранилище GitHub недоступно")
        if len(response.content) > MAX_BYTES * 2:
            raise ValueError("Превышен размер ответа GitHub")
        return response.json()

    def _blob(self, sha: str) -> bytes:
        if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
            raise ValueError("Некорректный SHA blob")
        blob = self.request("GET", "git/blobs/" + sha)
        if blob.get("encoding") != "base64":
            raise ValueError("Неизвестная кодировка GitHub blob")
        raw = _decode_base64(blob.get("content"), "Повреждённый GitHub blob")
        if len(raw) > MAX_BYTES:
            raise ValueError("Превышен размер GitHub blob")
        return raw

    def load(self) -> tuple[dict, str | None]:
        ref = self.request("GET", "git/ref/heads/" + BRANCH, missing=True)
        if ref is None:
            return initial_state(), None
        sha = ref.get("object", {}).get("sha")
        if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
            raise ValueError("Некорректный SHA ветки очереди")
        commit = self.request("GET", "git/commits/" + sha)
        tree_sha = commit.get("tree", {}).get("sha")
        tree = self.request("GET", "git/trees/" + str(tree_sha))
        entries = {row.get("path"): row for row in tree.get("tree", []) if isinstance(row, dict)}
        if tree.get("truncated") or set(entries) != FILES:
            raise ValueError("Ветка очереди содержит чужие файлы")
        raw: dict[str, bytes] = {}
        for path in FILES:
            entry = entries[path]
            if entry.get("type") != "blob" or entry.get("mode") != "100644":
                raise ValueError("Ветка очереди имеет неверную структуру")
            raw[path] = self._blob(entry["sha"])
        if raw[".carousel-cover-background-data"].decode("utf-8") != MARKER:
            raise ValueError("Неверный marker ветки очереди")
        try:
            vercel = json.loads(raw["vercel.json"])
            state = json.loads(raw["state.json"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Повреждённая ветка очереди") from None
        if vercel != {"git": {"deploymentEnabled": False}}:
            raise ValueError("Ветка очереди не защищена от деплоя")
        validate_state(state)
        return upgrade_state(state), sha

    def save(self, state: dict, expected_sha: str | None) -> str:
        if not self.token:
            raise ValueError("Для записи нужен GitHub token")
        text = validate_state(state)
        tree = self.request("POST", "git/trees", json={"tree": [
            {"path": "state.json", "mode": "100644", "type": "blob", "content": text},
            {"path": "vercel.json", "mode": "100644", "type": "blob", "content": '{"git":{"deploymentEnabled":false}}'},
            {"path": ".carousel-cover-background-data", "mode": "100644", "type": "blob", "content": MARKER},
        ]})
        commit = self.request("POST", "git/commits", json={
            "message": "Обновить очередь перевода обложек",
            "tree": tree["sha"],
            "parents": [expected_sha] if expected_sha else [],
        })
        if expected_sha:
            self.request("PATCH", "git/refs/heads/" + BRANCH,
                         json={"sha": commit["sha"], "force": False}, conflict=True)
        else:
            self.request("POST", "git/refs", json={"ref": "refs/heads/" + BRANCH,
                         "sha": commit["sha"]}, conflict=True)
        return commit["sha"]


# Источники уже согласованного каталога; watchdog читает только SHA, без контента.
SOURCES = {
    "archive7d": ("data/authors-carousels-20261013-20261019", "activity-report.json"),
    "archive14d": ("data/authors-carousels-20261006-20261019", "activity-report.json"),
    "live": ("data/carousels-favorites-live", "state.json"),
}


def source_refs(store) -> dict:
    refs = {}
    for name, (branch, path) in SOURCES.items():
        # Git tree содержит только метаданные: полный JSON исходника не скачивается.
        value = store.request("GET", "git/trees/" + branch)
        if not isinstance(value, dict) or value.get("truncated") or not isinstance(value.get("tree"), list):
            raise ValueError("Некорректное дерево источника")
        entry = next((row for row in value["tree"] if isinstance(row, dict) and row.get("path") == path), {})
        sha = entry.get("sha") if entry.get("type") == "blob" else None
        if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
            raise ValueError("Некорректный SHA источника")
        refs[name] = sha
    return refs
