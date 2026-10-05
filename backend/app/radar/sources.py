"""Источники разведки Радара: разбор ввода, добавление, исключение, возврат
(docs/specs/10-radar-intel.md, «Источники»). Apify не вызывается."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from . import intel_settings as cfg
from . import repo_intel

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SPLIT_RE = re.compile(r"[,\s]+")
# Ссылки на конкретные материалы, а не на профиль.
_NON_PROFILE_PATHS = {"reel", "reels", "p", "tv", "stories", "explore"}


class SourceLimitError(Exception):
    """Превышен MAX_SOURCES — роутер отдаёт 400."""


class SourceNotFoundError(Exception):
    """Нет такого источника / исключённого — роутер отдаёт 404."""


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def _parse_token(token: str) -> tuple[str, str]:
    """Один токен → (ник, '') или ('', причина отказа)."""
    raw = token.strip()
    low = raw.lower()
    if "instagram.com" in low or low.startswith(("http://", "https://")):
        url = raw if "://" in raw else "https://" + raw
        parts = [p for p in urlparse(url).path.split("/") if p]
        if not parts:
            return "", "в ссылке нет ника профиля"
        if parts[0].lower() in _NON_PROFILE_PATHS:
            return "", "это ссылка на материал, а не на профиль"
        name = parts[0]
    else:
        name = raw
    name = name.lstrip("@")
    if not _USERNAME_RE.match(name):
        return "", "недопустимый ник (латиница, цифры, точка и подчёркивание, до 30 знаков)"
    return name.lower(), ""


def parse_input(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Строка со значениями через запятую/пробел/перенос → (ники без дублей, отклонённые)."""
    usernames: list[str] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in _SPLIT_RE.split(text or ""):
        if not token:
            continue
        name, reason = _parse_token(token)
        if reason:
            invalid.append({"input": token, "reason": reason})
        elif name not in seen:
            seen.add(name)
            usernames.append(name)
    return usernames, invalid


def add_sources(text: str) -> dict[str, Any]:
    """Добавляет источники. Лимит проверяется ДО любых записей: при превышении не пишется ничего."""
    usernames, invalid = parse_input(text)
    existing = repo_intel.get_competitors(usernames)
    to_add: list[str] = []
    to_restore: list[str] = []
    already: list[str] = []
    for u in usernames:
        row = existing.get(u)
        if row and row.get("is_source"):
            already.append(u)
        elif row and row.get("excluded_at"):
            to_restore.append(u)
        else:
            to_add.append(u)
    if to_add or to_restore:
        active = repo_intel.count_active_sources()
        if active + len(to_add) + len(to_restore) > cfg.MAX_SOURCES:
            raise SourceLimitError(
                f"Лимит источников — {cfg.MAX_SOURCES}. Сейчас {active}, добавить ещё "
                f"{len(to_add) + len(to_restore)} нельзя: уберите лишние."
            )
    for u in to_add + to_restore:
        repo_intel.upsert_source(u)
    return {"added": to_add, "restored": to_restore, "already": already, "invalid": invalid}


def exclude_source(username: str) -> None:
    if not repo_intel.mark_excluded(normalize_username(username)):
        raise SourceNotFoundError("Такого источника нет")


def restore_source(username: str) -> None:
    name = normalize_username(username)
    row = repo_intel.get_competitors([name]).get(name)
    if not row or row.get("is_source") or not row.get("excluded_at"):
        raise SourceNotFoundError("Такого исключённого источника нет")
    if repo_intel.count_active_sources() >= cfg.MAX_SOURCES:
        raise SourceLimitError(f"Лимит источников — {cfg.MAX_SOURCES}: сначала уберите лишние.")
    if not repo_intel.mark_restored(name):
        raise SourceNotFoundError("Такого исключённого источника нет")
