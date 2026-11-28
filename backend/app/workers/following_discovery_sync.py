"""Публикует проверенный снимок подписок в связанном проекте Supabase."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NamedTuple

import httpx

from app.radar.following_discovery import DiscoveryError, Snapshot, normalize_input
from app.radar.following_storage import (
    DEFAULT_BUCKET,
    decode_snapshot,
    storage_project_url,
)

_UNCHANGED = "Облачный снимок уже актуален."
_STALE = "В хранилище уже есть более новая версия снимка."
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


class Signature(NamedTuple):
    device: int
    inode: int
    size: int
    mtime_ns: int


def _signature(stat: os.stat_result) -> Signature:
    return Signature(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _content(snapshot: Snapshot) -> bytes:
    return snapshot.model_dump_json().encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stamp(value: datetime | str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _revision(snapshot: Snapshot) -> tuple[datetime, datetime, int]:
    dates = [_stamp(row.profile_checked_at) for row in snapshot.rows if row.profile_checked_at]
    checked = sum(bool(row.profile_checked_at) or row.profile_status in ("available", "unavailable", "private")
                  for row in snapshot.rows)
    return _stamp(snapshot.checked_at), max(dates, default=_EARLIEST), checked


def _is_older(candidate: Snapshot, current: Snapshot) -> bool:
    incoming, published = _revision(candidate), _revision(current)
    if incoming < published:
        return True
    if candidate.status == "running" and current.status != "running" and incoming[0] == published[0]:
        # Незавершённая версия того же списка должна приносить подтверждённый прогресс.
        return incoming <= published or incoming[2] < published[2]
    return False


def _project_dir(workdir: Path, url: httpx.URL) -> Path:
    data = workdir / "supabase" / ".temp"
    try:
        ref = (data / "project-ref").read_text(encoding="utf-8").strip()
    except OSError:
        raise DiscoveryError(503, "В указанном каталоге отсутствует привязка Supabase CLI.") from None
    if not re.fullmatch(r"[a-z0-9]{20}", ref) or url.host != f"{ref}.supabase.co" or url.port not in (None, 443):
        raise DiscoveryError(503, "Связанный проект Supabase CLI отличается от адреса SUPABASE_URL.")
    return data


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DiscoveryError(429, "Этот снимок уже синхронизирует другой процесс.") from None
        yield
    finally:
        os.close(descriptor)


class Published(NamedTuple):
    snapshot: Snapshot
    digest: str


class History:
    def __init__(self, path: Path):
        self.path = path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = {"seen": [], "latest": None}
        except ValueError:
            raise DiscoveryError(503, "Локальная история синхронизации повреждена.") from None
        seen = value.get("seen") if isinstance(value, dict) else None
        latest = value.get("latest") if isinstance(value, dict) else None
        if (not isinstance(seen, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in seen)
                or (latest is not None and latest not in seen)):
            raise DiscoveryError(503, "Локальная история синхронизации повреждена.")
        self.digests = set(seen)
        self.latest = latest

    def remember(self, digest: str) -> None:
        if self.latest == digest:
            return
        updated = self.digests | {digest}
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.path.parent, prefix=".discovery-sync-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps({"seen": sorted(updated), "latest": digest}).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        self.digests = updated
        self.latest = digest


class LinkedStorage:
    def __init__(self, binary: str, workdir: Path, url: httpx.URL):
        self.binary = binary
        self.workdir = workdir
        self.url = url

    def _copy(self, source: str, destination: str, *, upload: bool) -> bool:
        _project_dir(self.workdir, self.url)
        command = [self.binary, "storage", "cp", source, destination,
                   "--linked", "--experimental", "--workdir", str(self.workdir), "--output-format", "json"]
        if upload:
            command.extend(["--cache-control", "no-cache, no-store, max-age=0", "--content-type", "application/json"])
        try:
            result = subprocess.run(command, timeout=45, stdin=subprocess.DEVNULL, capture_output=True,
                                    env={**os.environ, "DO_NOT_TRACK": "1"}, check=False)
        except (OSError, subprocess.SubprocessError):
            raise DiscoveryError(503, "Supabase CLI временно не подтверждает доступ к хранилищу.") from None
        if result.returncode == 0:
            return True
        diagnostic = result.stdout + b"\n" + result.stderr
        if not upload and re.search(rb"\bNoSuchKey\b", diagnostic, re.IGNORECASE):
            return False
        raise DiscoveryError(503, "Supabase CLI временно не подтверждает доступ к хранилищу.")

    def _object(self, username: str) -> str:
        return f"ss:///{DEFAULT_BUCKET}/snapshots/{username}.json"

    def read(self, username: str) -> Published | None:
        with TemporaryDirectory(prefix="discovery-sync-read-") as directory:
            projection = Path(directory) / f"{username}.json"
            if not self._copy(self._object(username), str(projection), upload=False):
                return None
            content = projection.read_bytes()
            return Published(decode_snapshot(content, username), _digest(content))

    def upload(self, username: str, content: bytes) -> None:
        with TemporaryDirectory(prefix="discovery-sync-write-") as directory:
            projection = Path(directory) / f"{username}.json"
            projection.write_bytes(content)
            self._copy(str(projection), self._object(username), upload=True)


def _sync(path: Path, signature: Signature, username: str, storage: LinkedStorage, history: History) -> str:
    with path.open("rb") as stream:
        raw = stream.read()
        if _signature(os.fstat(stream.fileno())) != signature:
            raise DiscoveryError(409, "Локальный снимок обновился во время чтения.")
    snapshot = decode_snapshot(raw, username)
    content = _content(snapshot)
    digest = _digest(content)
    published = storage.read(username)
    if published:
        if published.digest == digest:
            history.remember(digest)
            return _UNCHANGED
        previous = _digest(_content(published.snapshot))
        if previous != digest:
            if digest in history.digests or _is_older(snapshot, published.snapshot):
                return _STALE
            history.remember(previous)
    elif digest in history.digests and digest != history.latest:
        return _STALE
    if _signature(path.stat()) != signature:
        raise DiscoveryError(409, "Локальный снимок обновился во время сверки.")
    storage.upload(username, content)
    stored = storage.read(username)
    if not stored or stored.digest != digest:
        raise DiscoveryError(503, "Облачный снимок ещё не подтвердил контрольную сумму. Сверка повторится.")
    history.remember(digest)
    return f"Снимок @{username} обновлён. Подписок {snapshot.collected}, SHA-256 {digest[:12]}."


def _run(path: Path, workdir: Path, binary: str, *, watch: bool, poll_seconds: float) -> None:
    username = path.stem
    if path.suffix != ".json" or normalize_input(username) != username:
        raise DiscoveryError(400, "Ожидается имя файла вида username.json с нормализованным ником.")
    url = storage_project_url()
    state_dir = _project_dir(workdir, url)
    state_path = state_dir / f"discovery-sync-{username}.json"
    storage = LinkedStorage(binary, workdir, url)
    with _locked(state_path.with_suffix(".lock")):
        history = History(state_path)
        verified = None
        previous_error = None
        while True:
            try:
                signature = _signature(path.stat())
                if signature != verified:
                    print(_sync(path, signature, username, storage, history), flush=True)
                    verified = signature
                    previous_error = None
            except (DiscoveryError, OSError) as exc:
                detail = exc.message if isinstance(exc, DiscoveryError) else "Локальный снимок временно недоступен."
                if not watch:
                    raise DiscoveryError(503, detail) from None
                if detail != previous_error:
                    print(detail, flush=True)
                    previous_error = detail
            if not watch:
                return
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Путь к файлу username.json")
    parser.add_argument("--workdir", type=Path, required=True, help="Корень связанного проекта Supabase")
    parser.add_argument("--supabase-cli", default="supabase", help="Путь к установленному Supabase CLI")
    parser.add_argument("--watch", action="store_true", help="Отслеживать изменения снимка и повторять сверку после сбоя")
    parser.add_argument("--poll-seconds", type=float, default=5, help="Интервал локальной проверки, в секундах")
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 60:
        parser.error("Интервал проверки должен быть от 1 до 60 секунд")
    binary = shutil.which(args.supabase_cli)
    if not binary:
        parser.error("Supabase CLI недоступен. Укажите путь через --supabase-cli")
    try:
        _run(args.snapshot.expanduser().absolute(), args.workdir.expanduser().resolve(), binary,
             watch=args.watch, poll_seconds=args.poll_seconds)
    except DiscoveryError as exc:
        print(exc.message, flush=True)
        return 1
    except OSError:
        print("Недоступен локальный файл или каталог синхронизации.", flush=True)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
