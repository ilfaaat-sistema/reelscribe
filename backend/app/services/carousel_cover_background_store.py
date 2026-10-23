"""CAS-хранилище очереди фонового перевода обложек в отдельной ветке GitHub."""
from __future__ import annotations

import base64
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
STATUSES = {"pending", "inflight", "running", "ready", "no_text", "review", "unknown", "gone"}
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
        "version": 1, "mode": mode, "updated_at": datetime.now(timezone.utc).isoformat(),
        "inventory": None, "items": [], "pause_until": None, "pause_reason": None,
        "consecutive_errors": 0,
        "counters": {"posts": 0, "ready": 0, "skipped": 0, "errors": 0, "http_ms": 0},
    }


def validate_state(state: object) -> str:
    if not isinstance(state, dict) or set(state) != {
        "version", "mode", "updated_at", "inventory", "items", "pause_until", "pause_reason",
        "consecutive_errors", "counters",
    } or state.get("version") != 1 or state.get("mode") not in MODES:
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
    counters = state["counters"]
    if not isinstance(counters, dict) or set(counters) != {"posts", "ready", "skipped", "errors", "http_ms"} or any(type(v) is not int or v < 0 for v in counters.values()):
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
    def __init__(self, client, token: str | None = None):
        self.client = client
        self.token = token
        self.base = f"https://api.github.com/repos/{REPOSITORY}/"
        self.remaining: int | None = None
        self.reset_at: datetime | None = None

    def request(self, method: str, path: str, *, missing: bool = False, conflict: bool = False,
                accept: str = "application/vnd.github+json", **kwargs):
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.client.request(method, self.base + path, headers=headers, timeout=45, **kwargs)
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
        return state, sha

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
