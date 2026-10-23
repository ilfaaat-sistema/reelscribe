"""CAS-хранилище очереди массового перевода в отдельной ветке GitHub."""
from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timezone

from app.services.carousel_caption_rules import CAPTION_RULES_VERSION

REPOSITORY = "ilfaaat-sistema/reelscribe"
BRANCH = "data/carousel-caption-bulk"
MAX_BYTES = 10 * 1024 * 1024
MARKER = "Данные очереди перевода подписей каруселей; код приложения отсутствует.\n"
FILES = {"state.json", "vercel.json", ".carousel-caption-bulk-data"}
PERIODS = {"7d", "14d", "30d", "archive14d"}
STATUSES = {
    "pending", "inflight", "ready", "not_needed", "empty", "too_long", "gone",
    "source_changed", "review", "unknown", "error",
}
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


def initial_state() -> dict:
    return {
        "version": 1,
        "classification_version": CAPTION_RULES_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "discovered_day": None,
        "refs": {},
        "items": [],
        "inventory": {"empty": 0, "not_needed": 0, "too_long": 0, "eligible": 0},
        "pause_until": None,
        "pause_reason": None,
        "consecutive_translation_failures": 0,
    }


def validate_state(state: object) -> str:
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Неподдерживаемая версия очереди")
    required = {
        "version", "updated_at", "discovered_day", "refs", "items", "inventory", "pause_until",
        "pause_reason", "consecutive_translation_failures",
    }
    if set(state) not in (required, required | {"classification_version"}):
        raise ValueError("Некорректные поля очереди")
    classification_version = state.get("classification_version", 1)
    if type(classification_version) is not int or classification_version < 1:
        raise ValueError("Некорректная версия классификации")
    _iso(state["updated_at"], nullable=False)
    discovered_day = state["discovered_day"]
    if discovered_day is not None:
        try:
            parsed_day = date.fromisoformat(discovered_day)
        except (TypeError, ValueError):
            raise ValueError("Некорректный день инвентаризации") from None
        if parsed_day.isoformat() != discovered_day:
            raise ValueError("Некорректный день инвентаризации")
    _iso(state["pause_until"])
    if state["pause_reason"] is not None and (
        not isinstance(state["pause_reason"], str) or not _SAFE_CODE.fullmatch(state["pause_reason"])
    ):
        raise ValueError("Некорректная причина паузы")
    failures = state["consecutive_translation_failures"]
    if type(failures) is not int or not 0 <= failures <= 3:
        raise ValueError("Некорректный счётчик ошибок")
    refs = state["refs"]
    if not isinstance(refs, dict) or len(refs) > 4:
        raise ValueError("Некорректные refs")
    for name, ref in refs.items():
        if name not in {"archive7d", "archive14d", "live"} or not isinstance(ref, dict) or set(ref) != {"commit", "blob"}:
            raise ValueError("Некорректный ref источника")
        if not all(isinstance(ref[key], str) and _GIT_SHA.fullmatch(ref[key]) for key in ("commit", "blob")):
            raise ValueError("Некорректный SHA источника")
    inventory = state["inventory"]
    if not isinstance(inventory, dict) or set(inventory) != {"empty", "not_needed", "too_long", "eligible"}:
        raise ValueError("Некорректная сводка инвентаря")
    if any(type(value) is not int or value < 0 for value in inventory.values()):
        raise ValueError("Некорректный счётчик инвентаря")
    items = state["items"]
    if not isinstance(items, list) or len(items) > 100_000:
        raise ValueError("Некорректный размер очереди")
    seen: set[tuple[str, str]] = set()
    allowed = {"id", "source_hash", "periods", "status", "updated_at", "code", "inflight_started_at"}
    for item in items:
        if not isinstance(item, dict) or set(item) != allowed:
            raise ValueError("Некорректная запись очереди")
        identity, digest = item["id"], item["source_hash"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError("Некорректный ID очереди")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest) or (identity, digest) in seen:
            raise ValueError("Некорректный или повторный hash")
        seen.add((identity, digest))
        periods = item["periods"]
        if not isinstance(periods, list) or not periods or len(periods) != len(set(periods)) or any(p not in PERIODS for p in periods):
            raise ValueError("Некорректные периоды очереди")
        if item["status"] not in STATUSES:
            raise ValueError("Некорректный статус очереди")
        _iso(item["updated_at"], nullable=False)
        inflight = _iso(item["inflight_started_at"])
        if item["status"] == "inflight" and inflight is None:
            raise ValueError("inflight без времени старта")
        code = item["code"]
        if code is not None and (not isinstance(code, str) or not _SAFE_CODE.fullmatch(code)):
            raise ValueError("Некорректный код очереди")
    text = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    # SHA уже проверены по точному lowercase hex-формату выше. Только эти поля
    # исключаем из эвристики credentials: подстрока eaa в SHA не является Meta token.
    # Все остальные значения и имена полей проверяются прежним строгим сканером.
    scan_state = {
        **state,
        "refs": {name: {"commit": None, "blob": None} for name in refs},
        "items": [{**item, "source_hash": None} for item in items],
    }
    scan_text = json.dumps(scan_state, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if _SECRET.search(scan_text):
        raise ValueError("В очереди обнаружены запрещённые данные")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Превышен размер очереди")
    return text


class CarouselCaptionBulkStore:
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

    def source_ref(self, branch: str, path: str) -> tuple[str, str]:
        """Возвращает commit/blob через Contents API, не доверяя download_url."""
        value = self.request("GET", f"contents/{path}?ref={branch}", accept="application/vnd.github.object+json")
        if value.get("type") != "file" or not _GIT_SHA.fullmatch(str(value.get("sha", ""))):
            raise ValueError("Источник GitHub не является файлом")
        ref = self.request("GET", "git/ref/heads/" + branch)
        commit = ref.get("object", {}).get("sha")
        if not isinstance(commit, str) or not _GIT_SHA.fullmatch(commit):
            raise ValueError("Некорректный commit источника")
        return commit, value["sha"]

    def read_source_json(self, branch: str, path: str, expected_blob: str) -> object:
        value = self.request("GET", f"contents/{path}?ref={branch}", accept="application/vnd.github.object+json")
        if value.get("type") != "file" or value.get("sha") != expected_blob:
            raise Conflict("Источник изменился во время чтения")
        if value.get("encoding") == "base64":
            raw = _decode_base64(value.get("content"), "Повреждённый Contents response")
        elif value.get("encoding") == "none":
            raw = self._blob(expected_blob)
        else:
            raise ValueError("Неизвестная кодировка Contents response")
        if len(raw) > MAX_BYTES:
            raise ValueError("Превышен размер источника")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Источник не является JSON") from None

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
        if raw[".carousel-caption-bulk-data"].decode("utf-8") != MARKER:
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
            {"path": ".carousel-caption-bulk-data", "mode": "100644", "type": "blob", "content": MARKER},
        ]})
        commit = self.request("POST", "git/commits", json={
            "message": "Обновить очередь перевода подписей",
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
