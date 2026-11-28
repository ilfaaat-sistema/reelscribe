"""Снимки подписок и возобновляемый локальный поиск авторов."""
from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.radar import following_apify as apify
from app.radar import following_meta as meta

_USERNAME = re.compile(r"(?=.{1,30}\Z)[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*")
_REEL = re.compile(r"/(?:[A-Za-z0-9._]+/)?(reel|p)/([A-Za-z0-9_-]{1,64})/?")
_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_RESERVED_PATHS = {"accounts", "explore", "reel", "reels", "stories", "p", "tv", "share"}
_IG_URL = "https://www.instagram.com/"
_POLL_DELAY = 3
_META_BATCH = 8
_META_WINDOW = 8
_RUNNING_STATUSES = {"READY", "RUNNING", "ABORTING", "TIMING-OUT"}
_STOPPED_STATUSES = {"SUCCEEDED", "ABORTED", "FAILED", "TIMED-OUT"}
_UNKNOWN_START = "Apify не подтвердил запуск. Перед повтором нужно проверить состояние предыдущего запуска."
_META_RATE = "Достигнут лимит Meta. Обогащение продолжится после паузы."
_META_PAUSE = "Meta временно недоступна. Обогащение продолжится после паузы."
_COUNTER = Annotated[int, Field(strict=True, ge=0)]
Status = Literal["running", "complete", "partial", "failed"]


class DiscoveryError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _username(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _USERNAME.fullmatch(value) else None


def _instagram_url(value: str):
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    return url if url.scheme in {"https", "http"} and url.netloc.lower() in _IG_HOSTS else None


def normalize_input(value: str) -> str:
    raw = value.strip()
    if raw.partition("/")[0].lower() in _IG_HOSTS:
        raw = "https://" + raw
    if "://" in raw:
        url = _instagram_url(raw)
        path = url.path.removeprefix("/").removesuffix("/") if url else ""
        name = _username(path) if path.lower() not in _RESERVED_PATHS else None
    else:
        name = _username(raw.removeprefix("@"))
    if not name:
        raise DiscoveryError(400, "Введите ник или ссылку на профиль Instagram.")
    return name


class Row(BaseModel):
    username: str
    name: str | None = None
    bio: str | None = None
    followers: _COUNTER | None = None
    following: _COUNTER | None = None
    posts: _COUNTER | None = None
    private: Annotated[bool, Field(strict=True)] | None = None
    profile_url: str = ""
    reel_url: str | None = None
    reel_date: datetime | date | None = None
    profile_source: str | None = None
    profile_checked_at: str | None = None
    profile_status: Literal["available", "private", "unavailable", "pending", "not_checked"] | None = None

    @field_validator("profile_checked_at")
    @classmethod
    def check_profile_date(cls, value: str | None) -> str | None:
        if value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def check_links(self) -> Row:
        name = _username(self.username)
        if not name:
            raise ValueError("Некорректный ник строки")
        self.username = name
        self.profile_url = f"{_IG_URL}{name}/"
        if self.reel_url:
            url = _instagram_url(self.reel_url)
            match = _REEL.fullmatch(url.path) if url else None
            self.reel_url = f"{_IG_URL}{match.group(1)}/{match.group(2)}/" if match else None
        if not self.reel_url:
            self.reel_date = None
        return self


class Snapshot(BaseModel):
    username: str
    status: Status
    expected: _COUNTER | None
    collected: _COUNTER
    checked_at: datetime
    reason: str | None
    rows: list[Row]

    @model_validator(mode="after")
    def check_total(self) -> Snapshot:
        if self.collected != len(self.rows) or len({row.username for row in self.rows}) != len(self.rows):
            raise ValueError("Число уникальных строк не соответствует снимку")
        if self.status == "complete" and self.expected != self.collected:
            raise ValueError("Полный результат должен соответствовать счётчику донора")
        return self


class ActiveRun(BaseModel):
    username: str
    token_ref: str
    run_id: str | None = None
    checked_at: datetime
    retry_at: datetime | None = None
    list_checked_at: datetime | None = None
    reason: str | None = None
    phase: Literal["following", "meta"] = "following"
    expected: _COUNTER | None = None
    list_reason: str | None = None
    rows: list[Row] = Field(default_factory=list)
    next_row: int = 0
    attempts: int = 0

    @field_validator("username")
    @classmethod
    def check_username(cls, name: str) -> str:
        if _username(name) != name:
            raise ValueError("Некорректный ник активного запуска")
        return name

    def snapshot(self, status: Status = "running", reason: str | None = None) -> Snapshot:
        return Snapshot(username=self.username, status=status, expected=self.expected, collected=len(self.rows),
                        checked_at=self.list_checked_at or self.checked_at, reason=reason or self.reason, rows=self.rows)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.active_path = directory / ".discovery-active.json"

    def _read(self, path: Path, model):
        try:
            if path.is_symlink():
                raise OSError()
            return model.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            raise DiscoveryError(503, "Не удалось прочитать сохранённое состояние поиска авторов.") from None

    def snapshot(self, username: str) -> Snapshot | None:
        snapshot = self._read(self.directory / f"{username}.json", Snapshot)
        if snapshot and _username(snapshot.username) != username:
            raise DiscoveryError(503, "Имя донора в сохранённом списке не совпадает с запросом.")
        return snapshot

    def active(self) -> ActiveRun | None:
        return self._read(self.active_path, ActiveRun)

    def write(self, path: Path, value: BaseModel, *, create: bool = False) -> None:
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.directory, prefix=".discovery-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(value.model_dump_json().encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if create:
                # Импорт может параллельно положить независимый снимок того же донора.
                os.link(temporary, path)
            else:
                temporary.replace(path)
        except FileExistsError:
            if not create:
                raise
        except OSError:
            raise DiscoveryError(503, "Не удалось сохранить состояние поиска авторов.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory / ".discovery.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise DiscoveryError(503, "Каталог снимков недоступен для записи.") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def finish(self, run: ActiveRun, explanation: str | None = None) -> Snapshot:
        if explanation:
            for row in run.rows[run.next_row:]:
                if row.profile_status == "pending":
                    row.profile_status = "not_checked"
        unavailable = sum(row.profile_status in ("private", "unavailable") for row in run.rows)
        notes = [run.list_reason, explanation]
        if unavailable:
            notes.append(f"Карточки недоступны или закрыты у {unavailable} из {len(run.rows)} профилей.")
        if run.list_reason:
            status = "partial" if run.rows else "failed"
        else:
            status = "complete"
        run.reason = None
        snapshot = run.snapshot(status, " ".join(note for note in notes if note) or None)
        self.write(self.directory / f"{run.username}.json", snapshot, create=True)
        self.active_path.unlink(missing_ok=True)
        return self.snapshot(run.username) or snapshot


def _store() -> Store | None:
    directory = os.environ.get("FOLLOWING_DISCOVERY_DIR", "").strip()
    return Store(Path(directory).expanduser()) if directory else None


def _cloud_snapshot(username: str) -> Snapshot | None:
    from app.radar.following_storage import read_snapshot

    return read_snapshot(username)


def _tokens(*, primary_only: bool = False) -> dict[str, str]:
    from app.core.config import settings
    from app.pipeline.shared_state import key_ref

    tokens = [settings.apify_api_token.strip()] if primary_only else settings.apify_token_list
    return {key_ref(token): token for token in tokens if token}


def _budget() -> tuple[int, Decimal]:
    try:
        limit = int(os.environ.get("FOLLOWING_DISCOVERY_MAX_PROFILES", "1500"))
        cap = Decimal(os.environ.get("FOLLOWING_DISCOVERY_MAX_CHARGE_USD", ""))
        if not 1 <= limit <= 3000 or not cap.is_finite() or cap <= 0:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise DiscoveryError(503, "Для нового сбора нужно задать серверный лимит подписок и бюджет Apify.") from None
    charge = apify.stage_charge(apify.LIST_RATE, limit)
    if charge > cap:
        raise DiscoveryError(429, "Заданный бюджет Apify меньше верхней оценки для лимита подписок.")
    return limit, charge


def start(username: str, *, allow_start: bool) -> Snapshot:
    store = _store()
    cached = (store.snapshot(username) if store else None) or _cloud_snapshot(username)
    if cached:
        return cached
    if not allow_start:
        raise DiscoveryError(403, "Новый сбор доступен только в локальном режиме с разрешением на запуск.")
    if not store:
        raise DiscoveryError(503, "На сервере не задан каталог снимков поиска авторов.")
    with store.lock() as acquired:
        if not acquired:
            raise DiscoveryError(429, "Поиск авторов уже обрабатывает другой запрос. Повторите через несколько секунд.")
        if cached := store.snapshot(username):
            return cached
        if active := store.active():
            if active.username == username:
                return active.snapshot() if active.run_id else active.snapshot("failed", _UNKNOWN_START)
            if active.run_id and store.snapshot(active.username):
                # Завершённый снимок уже записан, остался только служебный файл состояния.
                store.active_path.unlink(missing_ok=True)
            else:
                message = f"Сначала дождитесь результата поиска по @{active.username}." if active.run_id else _UNKNOWN_START
                raise DiscoveryError(429, message)
        limit, charge = _budget()
        primary = _tokens(primary_only=True)
        if not primary:
            raise DiscoveryError(503, "Для нового сбора не настроен основной серверный доступ к Apify.")
        token_ref, token = next(iter(primary.items()))
        with apify.Apify(token) as client:
            try:
                remaining = client.remaining_credit()
            except apify.ProviderError:
                raise DiscoveryError(502, "Не удалось проверить бесплатный остаток Apify. Попробуйте позже.") from None
            if remaining < charge:
                raise DiscoveryError(429, "Бесплатного остатка Apify недостаточно для настроенного лимита подписок.")
            active = ActiveRun(username=username, token_ref=token_ref, checked_at=_now())
            store.write(store.active_path, active)
            try:
                active.run_id = client.start(apify.LIST_ACTOR, {
                    "profiles": [username], "relationshipType": "FOLLOWING", "maxFollowingPerProfile": limit,
                    "includePrivateAccounts": True, "includeProfileMetrics": False,
                }, charge)
            except apify.ProviderError as exc:
                if exc.rejected:
                    store.active_path.unlink(missing_ok=True)
                    if exc.status in (402, 429):
                        raise DiscoveryError(429, "Apify отклонил запуск из-за лимита аккаунта.") from None
                    raise DiscoveryError(502, "Apify отклонил запуск. Проверьте доступ к сбору подписок.") from None
                active.reason = _UNKNOWN_START
                store.write(store.active_path, active)
                raise DiscoveryError(502, _UNKNOWN_START) from None
            store.write(store.active_path, active)
            return active.snapshot()


def _counter(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _section(item: dict, key: str) -> dict:
    section = item.get(key)
    if isinstance(section, dict):
        return section
    prefix = f"{key}."
    return {name.removeprefix(prefix): value for name, value in item.items() if name.startswith(prefix)}


def _list_result(run: ActiveRun, dataset: list[dict], status: str) -> None:
    rows: dict[str, Row] = {}
    identities = set()
    summaries = []
    summary_status = None
    for item in dataset:
        source = _section(item, "sourceProfile")
        relationship = item.get("relationshipType")
        if _username(source.get("username")) != run.username:
            if item.get("rowType") == "error" and _username(item.get("input")) == run.username:
                summary_status = item.get("status")
            continue
        if relationship != "FOLLOWING":
            continue
        if item.get("rowType") == "summary":
            summaries.append(item)
            continue
        user = _section(item, "user")
        if item.get("rowType") != "relationship" or item.get("status") != "SUCCESS" or not user:
            continue
        name = _username(user.get("username"))
        identity = user.get("id")
        identity = identity if isinstance(identity, str) else None
        if not name or name in rows or (identity and identity in identities):
            continue
        if identity:
            identities.add(identity)
        private = user.get("isPrivate")
        private = private if type(private) is bool else None
        rows[name] = Row(username=name, name=_text(user.get("fullName")), private=private,
                         profile_status="private" if private else "pending")

    run.rows = list(rows.values())
    run.phase = "meta"
    run.list_checked_at = run.checked_at
    if len(summaries) == 1:
        summary = summaries[0]
        source = _section(summary, "sourceProfile")
        coverage = _section(summary, "coverage")
        scraped_at = _text(summary.get("scrapedAt"))
        if scraped_at:
            try:
                run.list_checked_at = datetime.fromisoformat(scraped_at.replace("Z", "+00:00"))
            except ValueError:
                pass
        run.expected = _counter(source.get("followingCount"))
        coverage_count = _counter(coverage.get("listCount"))
        if run.expected is None:
            run.expected = coverage_count
        summary_status = summary.get("status")
        if (status == "SUCCEEDED" and summary_status in ("SUCCESS", "NO_RELATIONSHIPS")
                and summary.get("coverageStatus") == "FULL_AVAILABLE_SET" and coverage.get("stopReason") == "end"
                and run.expected == len(rows) and coverage_count in (None, run.expected)
                and _counter(coverage.get("rowsReturned")) == len(rows)):
            return
    if summary_status == "SOURCE_PRIVATE":
        run.list_reason = "Донор закрыл список подписок."
    elif summary_status in ("PROFILE_NOT_FOUND", "INVALID_PROFILE"):
        run.list_reason = "Поставщик не нашёл профиль донора."
    elif status != "SUCCEEDED":
        run.list_reason = "Apify завершил сбор досрочно. Сохранены доступные подписки."
    elif run.expected is None:
        run.list_reason = "Поставщик не подтвердил счётчик подписок донора."
    else:
        run.list_reason = "Поставщик не подтвердил полноту списка подписок."


def _pause(run: ActiveRun, reason: str, seconds: int) -> None:
    run.reason = reason
    run.retry_at = _now() + timedelta(seconds=seconds)


def _meta_step(run: ActiveRun) -> tuple[bool, str | None]:
    """Возвращает признак завершения и причину досрочного завершения обогащения."""
    deadline = time.monotonic() + _META_WINDOW
    requests = 0
    try:
        with meta.Meta() as client:
            while run.next_row < len(run.rows) and requests < _META_BATCH and time.monotonic() < deadline:
                row = run.rows[run.next_row]
                if row.profile_status != "pending":
                    run.next_row += 1
                    continue
                result = client.lookup(row.username)
                requests += 1
                profile = result.values
                if profile is None:
                    row.profile_status = "unavailable"
                else:
                    row.profile_status = "available"
                    row.name = _text(profile.get("name")) or row.name
                    biography = _text(profile.get("biography"))
                    if biography is not None:
                        row.bio = biography
                    for field, key in (("followers", "followers_count"), ("following", "follows_count"), ("posts", "media_count")):
                        count = _counter(profile.get(key))
                        if count is not None:
                            setattr(row, field, count)
                    row.profile_source = "Meta Graph API"
                    row.profile_checked_at = _now().isoformat()
                run.next_row += 1
                run.attempts = 0
                if result.cooldown and run.next_row < len(run.rows):
                    _pause(run, _META_RATE, result.cooldown)
                    return False, None
    except meta.MetaError as exc:
        if exc.kind == "configuration":
            return True, "Часть карточек не проверена: на сервере не настроен доступ к Meta."
        if exc.kind == "auth":
            return True, "Часть карточек не проверена: Meta отклонила авторизацию или доступ к данным."
        if exc.kind == "rate":
            _pause(run, _META_RATE, exc.cooldown)
            return False, None
        run.attempts += 1
        if run.attempts >= 3:
            return True, "Часть карточек не проверена из-за технической ошибки Meta."
        _pause(run, _META_PAUSE, max(60 * run.attempts, exc.cooldown))
        return False, None
    checked = run.next_row
    run.reason = f"Meta проверила карточки {checked} из {len(run.rows)} профилей."
    run.retry_at = _now() + timedelta(seconds=_POLL_DELAY)
    return checked >= len(run.rows), None


def _advance_list(store: Store, run: ActiveRun) -> Snapshot:
    token = _tokens().get(run.token_ref)
    if not token:
        _pause(run, "Доступ к запуску Apify сейчас недоступен. Результат продолжит проверяться после восстановления доступа.", 60)
    else:
        try:
            with apify.Apify(token) as client:
                current = client.run(run.run_id)
                status = current.get("status")
                if status in _STOPPED_STATUSES:
                    dataset_id = current.get("defaultDatasetId")
                    data = client.items(dataset_id, apify.LIST_FIELDS, max_items=3020) if dataset_id else []
                    _list_result(run, data, status)
                    if not run.rows:
                        return store.finish(run)
                    run.reason = "Список получен. Начинается проверка карточек через Meta."
                    run.retry_at = None
                elif status in _RUNNING_STATUSES:
                    _pause(run, "Apify собирает список подписок.", _POLL_DELAY)
                else:
                    raise apify.ProviderError()
        except apify.ProviderError:
            _pause(run, "Apify пока не вернул результат. Повторная проверка продолжится автоматически.", 30)
    store.write(store.active_path, run)
    return run.snapshot()


def get(username: str, *, advance: bool) -> Snapshot | None:
    store = _store()
    if not store:
        return _cloud_snapshot(username)
    if cached := store.snapshot(username):
        return cached
    active = store.active()
    if not active or active.username != username:
        return _cloud_snapshot(username)
    if not advance:
        return active.snapshot("failed", _UNKNOWN_START) if active.reason == _UNKNOWN_START else active.snapshot()
    with store.lock() as acquired:
        if not acquired:
            return active.snapshot()
        if cached := store.snapshot(username):
            return cached
        active = store.active()
        if not active or active.username != username:
            return None
        if not active.run_id:
            return active.snapshot("failed", _UNKNOWN_START)
        now = _now()
        if active.retry_at and active.retry_at > now:
            return active.snapshot()
        active.checked_at = now
        if active.phase == "following":
            return _advance_list(store, active)
        done, explanation = _meta_step(active)
        if done:
            return store.finish(active, explanation)
        store.write(store.active_path, active)
        return active.snapshot()


def resume_meta(username: str, *, max_batches: int) -> tuple[Snapshot, datetime | None]:
    """Обновляет только непроверенные карточки существующего снимка через Meta."""
    store = _store()
    if not store:
        raise DiscoveryError(503, "На сервере не задан каталог снимков поиска авторов.")
    checkpoint = store.directory / f".{username}.meta.json"
    with store.lock() as acquired:
        if not acquired:
            raise DiscoveryError(429, "Снимок сейчас обрабатывается. Повторите через несколько секунд.")
        cached = store.snapshot(username)
        if not cached or cached.status == "running":
            raise DiscoveryError(404, "Сохранённый снимок донора пока недоступен.")
        run = store._read(checkpoint, ActiveRun)
        if not run or run.username != username or [row.username for row in run.rows] != [row.username for row in cached.rows]:
            run = ActiveRun(username=username, token_ref="", checked_at=_now(), phase="meta",
                            list_checked_at=cached.checked_at, expected=cached.expected, list_reason=cached.reason if cached.status != "complete" else None,
                            rows=cached.rows)
            for row in run.rows:
                if row.private is True:
                    row.profile_status = "private"
                elif row.profile_status is None and all(value is not None for value in (row.followers, row.following, row.posts)):
                    row.profile_status = "available"
                elif row.profile_status not in ("available", "unavailable"):
                    row.profile_status = "pending"
        for _ in range(max_batches):
            if run.retry_at and run.retry_at > _now():
                return cached, run.retry_at
            if not any(row.profile_status == "pending" for row in run.rows[run.next_row:]):
                break
            run.checked_at = _now()
            done, explanation = _meta_step(run)
            if done:
                if explanation:
                    for row in run.rows[run.next_row:]:
                        if row.profile_status == "pending":
                            row.profile_status = "not_checked"
                    run.reason = explanation
                run.retry_at = None
            elif run.reason not in (_META_PAUSE, _META_RATE):
                # CLI проверяет несколько небольших порций за вызов, сохраняя прогресс после каждой.
                run.retry_at = None
            notes = [run.list_reason, run.reason]
            unavailable = sum(row.profile_status in ("private", "unavailable") for row in run.rows)
            if done and unavailable:
                notes.append(f"Карточки недоступны или закрыты у {unavailable} из {len(run.rows)} профилей.")
            cached = run.snapshot(cached.status, " ".join(note for note in notes if note))
            store.write(checkpoint, run)
            store.write(store.directory / f"{username}.json", cached)
            if done:
                break
        if not any(row.profile_status == "pending" for row in run.rows[run.next_row:]):
            checkpoint.unlink(missing_ok=True)
        return cached, run.retry_at
