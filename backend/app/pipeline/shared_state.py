"""Общее состояние воркеров в базе: cooldown API-ключей и кэш подписчиков.

Зачем модуль нужен: и cooldown, и кэш подписчиков раньше жили в словарях внутри процесса.
Пока воркер был один, это работало. Когда воркеров стало несколько (matrix в worker.yml),
каждый видел свою картину — независимо упирался в исчерпанный ключ и заново скрейпил профиль
уже известного автора. Профильный скрейп самый дорогой в каскаде, поэтому расход рос примерно
пропорционально числу воркеров (замер 10.08.2026: с $0.0188 до $0.053-0.075 за рилс).

Всё здесь **best-effort**: любая ошибка базы означает «данных нет», а не падение. Вызывающий код
в этом случае продолжает работать на своём словаре в памяти — ровно как до появления этого
модуля. Скачивание не должно вставать из-за того, что не ответила таблица со вспомогательным
состоянием.

Время хранится абсолютное (timestamptz). Прежний `time.monotonic()` для общей базы не годится
принципиально: это счётчик от старта процесса, и у каждого раннера он свой — сравнивать такие
значения между машинами бессмысленно.

Секреты в базу не попадают: ключи идентифицируются отпечатком `key_ref` (sha256, первые 16
символов), по которому восстановить ключ нельзя.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.db import get_db

logger = logging.getLogger(__name__)

MISS = object()   # «в кэше ничего нет» — отличим от закэшированного None

_COOLDOWN_TABLE = 'key_cooldown'
_FOLLOWERS_TABLE = 'followers_cache'


def key_ref(key: str) -> str:
    """Отпечаток ключа для базы. НЕ секрет: восстановить ключ из него нельзя."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Cooldown ключей ──────────────────────────────────────────────────────────────────────────

def _load_cooldowns_sync(provider: str) -> dict:
    db = get_db()
    rows = (
        db.table(_COOLDOWN_TABLE)
        .select('key_ref, actor, until')
        .eq('provider', provider)
        .gt('until', _now().isoformat())
        .execute()
    ).data or []
    out = {}
    for r in rows:
        until = r.get('until')
        if not until:
            continue
        # Postgres отдаёт ISO с таймзоной; '+00:00' и 'Z' оба валидны для fromisoformat в 3.11+,
        # но на 3.9 'Z' не парсится — заменяем явно.
        out[(r['key_ref'], r.get('actor') or '')] = datetime.fromisoformat(
            until.replace('Z', '+00:00')
        )
    return out


async def load_cooldowns(provider: str) -> dict:
    """Активные cooldown провайдера: {(key_ref, actor): until}. При ошибке базы — пустой dict."""
    try:
        return await asyncio.to_thread(_load_cooldowns_sync, provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Общий cooldown не прочитан (%s) — работаю по памяти процесса', exc)
        return {}


def _save_cooldown_sync(provider: str, ref: str, actor: str, until: datetime) -> None:
    db = get_db()
    db.table(_COOLDOWN_TABLE).upsert({
        'provider': provider,
        'key_ref': ref,
        'actor': actor,
        'until': until.isoformat(),
        'updated_at': _now().isoformat(),
    }).execute()


async def save_cooldown(provider: str, key: str, actor: str, minutes: int) -> None:
    """Поставить ключ на паузу до now+minutes. Ошибка базы не мешает работе — только логируется."""
    until = _now() + timedelta(minutes=minutes)
    try:
        await asyncio.to_thread(_save_cooldown_sync, provider, key_ref(key), actor, until)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Общий cooldown не записан (%s) — остаётся только в памяти процесса', exc)


# ── Кэш подписчиков ──────────────────────────────────────────────────────────────────────────

def _get_followers_sync(username: str) -> Any:
    db = get_db()
    rows = (
        db.table(_FOLLOWERS_TABLE)
        .select('followers, expires_at')
        .eq('username', username)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return MISS
    row = rows[0]
    expires_at = row.get('expires_at')
    if expires_at:
        exp = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if _now() >= exp:
            return MISS          # запись протухла — считаем, что её нет
    return row.get('followers')  # может быть None — это закэшированная неудача


async def get_followers(username: str) -> Any:
    """Подписчики из общего кэша, либо MISS. При ошибке базы — MISS (решает вызывающий)."""
    try:
        return await asyncio.to_thread(_get_followers_sync, username)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Общий кэш подписчиков не прочитан (%s)', exc)
        return MISS


def _set_followers_sync(username: str, count: Optional[int], expires_at: Optional[datetime]) -> None:
    db = get_db()
    db.table(_FOLLOWERS_TABLE).upsert({
        'username': username,
        'followers': count,
        'expires_at': expires_at.isoformat() if expires_at else None,
        'updated_at': _now().isoformat(),
    }).execute()


async def set_followers(username: str, count: Optional[int], fail_ttl_min: int) -> None:
    """Записать в общий кэш. Успех хранится бессрочно, неудача — с TTL до снятия cooldown."""
    expires_at = None if count is not None else _now() + timedelta(minutes=fail_ttl_min)
    try:
        await asyncio.to_thread(_set_followers_sync, username, count, expires_at)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Общий кэш подписчиков не записан (%s)', exc)
