"""Инструменты ИИ-чата (ТЗ 09) — читают базу и возвращают компактный JSON модели.

Поиск идёт на стороне Postgres через PostgREST-фильтр `imatch` (регистронезависимый POSIX
regex, `column=imatch.pattern`) — строки в Python НЕ выкачиваются целиком, фильтрация в базе.
`imatch` — метод отсутствует в билдере postgrest-py явным именем, но есть универсальный
`.filter(column, operator, value)`, который собирает `column=imatch.pattern` (проверено
09-2026: `postgrest._sync.request_builder.SyncFilterRequestBuilder.filter` кладёт
`f"{operator}.{criteria}"` как есть — оператор PostgREST может быть любым, включая `imatch`).

Каждая функция возвращает список компактных dict — без полных расшифровок (кроме `get_reel`).
"""
from __future__ import annotations

import re
import statistics
from typing import Any

from app.core.db import get_db

# ── Поля для search_text ────────────────────────────────────────────────────
# Ключ — имя поля, как его называет модель (и как оно упомянуто в ТЗ), значение — где искать.
# table: таблица PostgREST; column: колонка с текстом; kind: "reels" (прямое поле reels),
# "transcripts" (поле transcripts, метаданные рилса тянутся через embed reels(...)) или
# "radar" (radar_reels — свои поля, без залётности: там нет author_followers).

_REELS_META_COLS = "id,shortcode,url,author_handle,views,er,posted_at"

_FIELD_SPECS: dict[str, dict[str, str]] = {
    "reels.caption": {"table": "reels", "column": "caption", "kind": "reels"},
    "reels.caption_ru": {"table": "reels", "column": "caption_ru", "kind": "reels"},
    "transcripts.text": {"table": "transcripts", "column": "text", "kind": "transcripts"},
    "transcripts.text_ru": {"table": "transcripts", "column": "text_ru", "kind": "transcripts"},
    "transcripts.summary": {"table": "transcripts", "column": "summary", "kind": "transcripts"},
    "transcripts.ocr_text": {"table": "transcripts", "column": "ocr_text", "kind": "transcripts"},
    "radar_reels.caption": {"table": "radar_reels", "column": "caption", "kind": "radar"},
}

# Короткие алиасы без имени таблицы — модели проще писать "caption", чем "reels.caption".
_FIELD_ALIASES: dict[str, str] = {
    "caption": "reels.caption",
    "caption_ru": "reels.caption_ru",
    "text": "transcripts.text",
    "text_ru": "transcripts.text_ru",
    "summary": "transcripts.summary",
    "ocr_text": "transcripts.ocr_text",
}

SNIPPET_RADIUS = 120
MAX_TRANSCRIPT_CHARS = 6000


def _resolve_field(name: str) -> str | None:
    if name in _FIELD_SPECS:
        return name
    return _FIELD_ALIASES.get(name)


def _make_snippet(text: str | None, pattern: str) -> str | None:
    if not text:
        return None
    try:
        m = re.search(pattern, text, re.IGNORECASE)
    except re.error:
        m = None
    if not m:
        # Регекс на стороне Postgres мог найти совпадение (иной диалект regex), а Python re —
        # нет. В этом случае просто отдаём начало текста, лишь бы не падать.
        return text[: SNIPPET_RADIUS * 2].strip()
    start = max(0, m.start() - SNIPPET_RADIUS)
    end = min(len(text), m.end() + SNIPPET_RADIUS)
    snippet = text[start:end].strip()
    return snippet


def search_text(
    pattern: str, fields: list[str] | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    """Регулярное выражение POSIX без учёта регистра по полям reels/transcripts/radar_reels.

    Поиск целиком на стороне Postgres (PostgREST `imatch`), в Python — только вырезка сниппета.
    """
    db = get_db()
    limit = max(1, min(limit, 50))

    field_names = list(_FIELD_SPECS.keys())
    if fields:
        resolved = [_resolve_field(f) for f in fields]
        field_names = [f for f in resolved if f]
        if not field_names:
            field_names = list(_FIELD_SPECS.keys())

    results: list[dict[str, Any]] = []
    for field_name in field_names:
        spec = _FIELD_SPECS[field_name]
        try:
            if spec["kind"] == "reels":
                resp = (
                    db.table("reels")
                    .select(f"{_REELS_META_COLS},{spec['column']}")
                    .filter(spec["column"], "imatch", pattern)
                    .limit(limit)
                    .execute()
                )
                for row in resp.data or []:
                    results.append(
                        {
                            "source": "parser",
                            "id": row.get("id"),
                            "shortcode": row.get("shortcode"),
                            "url": row.get("url"),
                            "author": row.get("author_handle"),
                            "views": row.get("views"),
                            "er": row.get("er"),
                            "posted_at": row.get("posted_at"),
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
            elif spec["kind"] == "transcripts":
                resp = (
                    db.table("transcripts")
                    .select(f"reel_id,{spec['column']},reels({_REELS_META_COLS})")
                    .filter(spec["column"], "imatch", pattern)
                    .limit(limit)
                    .execute()
                )
                for row in resp.data or []:
                    reel = row.get("reels") or {}
                    results.append(
                        {
                            "source": "parser",
                            "id": reel.get("id"),
                            "shortcode": reel.get("shortcode"),
                            "url": reel.get("url"),
                            "author": reel.get("author_handle"),
                            "views": reel.get("views"),
                            "er": reel.get("er"),
                            "posted_at": reel.get("posted_at"),
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
            else:  # radar
                resp = (
                    db.table("radar_reels")
                    .select("id,username,url,views,posted_at,caption")
                    .filter(spec["column"], "imatch", pattern)
                    .limit(limit)
                    .execute()
                )
                for row in resp.data or []:
                    results.append(
                        {
                            "source": "radar",
                            "id": row.get("id"),
                            "shortcode": row.get("id"),
                            "url": row.get("url"),
                            "author": row.get("username"),
                            "views": row.get("views"),
                            "er": None,
                            "posted_at": row.get("posted_at"),
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 — один сбойный field не должен ронять весь поиск
            results.append({"error": f"поиск по {field_name} не удался: {exc}"})

    return results[:limit]


def filter_reels(
    author: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_views: int | None = None,
    min_er: float | None = None,
    sort_by: str = "views",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Фильтр по `reels` — автор, период, минимальные просмотры/залётность, сортировка."""
    db = get_db()
    limit = max(1, min(limit, 50))
    if sort_by not in ("views", "er", "posted_at", "likes"):
        sort_by = "views"

    q = db.table("reels").select(
        "id,shortcode,url,author_handle,views,likes,er,posted_at,caption"
    )
    if author:
        q = q.ilike("author_handle", f"%{author}%")
    if date_from:
        q = q.gte("posted_at", date_from)
    if date_to:
        q = q.lte("posted_at", date_to)
    if min_views is not None:
        q = q.gte("views", min_views)
    if min_er is not None:
        q = q.gte("er", min_er)
    q = q.order(sort_by, desc=True).limit(limit)

    try:
        resp = q.execute()
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"фильтр не выполнен: {exc}"}]

    out = []
    for row in resp.data or []:
        out.append(
            {
                "source": "parser",
                "id": row.get("id"),
                "shortcode": row.get("shortcode"),
                "url": row.get("url"),
                "author": row.get("author_handle"),
                "views": row.get("views"),
                "likes": row.get("likes"),
                "er": row.get("er"),
                "posted_at": row.get("posted_at"),
                "snippet": (row.get("caption") or "")[:300] or None,
            }
        )
    return out


def get_reel(shortcode: str) -> dict[str, Any]:
    """Карточка рилса + полная расшифровка (text_ru при наличии, иначе text), до 6000 симв."""
    db = get_db()
    try:
        resp = (
            db.table("reels")
            .select(
                "id,shortcode,url,caption,caption_ru,author_handle,author_followers,"
                "views,likes,comments,posted_at,er,lpf,cpf,eng"
            )
            .eq("shortcode", shortcode)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"рилс не найден: {exc}"}

    rows = resp.data or []
    if not rows:
        return {"error": f"рилс с shortcode={shortcode} не найден"}
    reel = rows[0]

    transcript_text = None
    try:
        t_resp = (
            db.table("transcripts")
            .select("text,text_ru,summary,tags,ocr_text,status")
            .eq("reel_id", reel["id"])
            .limit(1)
            .execute()
        )
        t_rows = t_resp.data or []
        if t_rows:
            t = t_rows[0]
            transcript_text = t.get("text_ru") or t.get("text")
            reel["summary"] = t.get("summary")
            reel["tags"] = t.get("tags")
            reel["ocr_text"] = t.get("ocr_text")
            reel["transcript_status"] = t.get("status")
    except Exception as exc:  # noqa: BLE001
        reel["transcript_error"] = str(exc)

    if transcript_text:
        reel["transcript"] = transcript_text[:MAX_TRANSCRIPT_CHARS]
    else:
        reel["transcript"] = None
    reel["source"] = "parser"
    return reel


def stats(
    group_by: str = "author",
    top: int = 15,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Число рилсов, сумма/медиана просмотров, средняя залётность — по автору или по месяцу.

    Считается в Python по выборке (2868 строк — допустимо), пагинация `.range()` по 1000 —
    серверный потолок PostgREST на строку ответа.
    """
    db = get_db()
    if group_by not in ("author", "month"):
        group_by = "author"
    top = max(1, min(top, 50))

    rows: list[dict[str, Any]] = []
    page_size = 1000
    offset = 0
    while True:
        q = db.table("reels").select("author_handle,views,er,posted_at")
        if date_from:
            q = q.gte("posted_at", date_from)
        if date_to:
            q = q.lte("posted_at", date_to)
        try:
            resp = q.range(offset, offset + page_size - 1).execute()
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"статистика не посчитана: {exc}"}]
        page = resp.data or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    groups: dict[str, dict[str, list[Any]]] = {}
    for row in rows:
        if group_by == "author":
            key = row.get("author_handle") or "?"
        else:
            posted = row.get("posted_at") or ""
            key = posted[:7] if posted else "?"  # YYYY-MM
        bucket = groups.setdefault(key, {"views": [], "er": []})
        if row.get("views") is not None:
            bucket["views"].append(row["views"])
        if row.get("er") is not None:
            bucket["er"].append(float(row["er"]))

    out = []
    for key, bucket in groups.items():
        views_list = bucket["views"]
        er_list = bucket["er"]
        out.append(
            {
                "group": key,
                "reels_count": len(views_list) or len(er_list),
                "views_sum": sum(views_list) if views_list else None,
                "views_median": statistics.median(views_list) if views_list else None,
                "er_avg": round(sum(er_list) / len(er_list), 2) if er_list else None,
            }
        )
    out.sort(key=lambda g: g["reels_count"], reverse=True)
    return out[:top]


# ── Схемы function calling (JSON Schema, передаются как parametersJsonSchema) ───────────────

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_text",
        "description": (
            "Полнотекстовый поиск по всей базе (регулярное выражение POSIX без учёта "
            "регистра). Сразу расширяй запрос вариантами написания и языками, например "
            "herm[eè]s|гермес|эрмес|хермес."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex POSIX без учёта регистра, можно с | для вариантов.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Необязательно: ограничить поля поиска. Значения: reels.caption, "
                        "reels.caption_ru, transcripts.text, transcripts.text_ru, "
                        "transcripts.summary, transcripts.ocr_text, radar_reels.caption. "
                        "Без указания — ищет по всем."
                    ),
                },
                "limit": {"type": "integer", "description": "Максимум результатов (default 20)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "filter_reels",
        "description": "Фильтр рилсов парсера по автору/периоду/просмотрам/залётности.",
        "parameters": {
            "type": "object",
            "properties": {
                "author": {"type": "string", "description": "Часть ника автора."},
                "date_from": {"type": "string", "description": "Дата ГГГГ-ММ-ДД, от."},
                "date_to": {"type": "string", "description": "Дата ГГГГ-ММ-ДД, до."},
                "min_views": {"type": "integer"},
                "min_er": {"type": "number"},
                "sort_by": {
                    "type": "string",
                    "enum": ["views", "er", "posted_at", "likes"],
                    "description": "По чему сортировать (default views).",
                },
                "limit": {"type": "integer", "description": "Максимум результатов (default 20)."},
            },
        },
    },
    {
        "name": "get_reel",
        "description": "Карточка рилса и его полная расшифровка по shortcode.",
        "parameters": {
            "type": "object",
            "properties": {
                "shortcode": {"type": "string", "description": "Код рилса из ссылки Instagram."},
            },
            "required": ["shortcode"],
        },
    },
    {
        "name": "stats",
        "description": "Агрегаты по автору или по месяцу: число рилсов, просмотры, залётность.",
        "parameters": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["author", "month"]},
                "top": {"type": "integer", "description": "Сколько групп вернуть (default 15)."},
                "date_from": {"type": "string", "description": "Дата ГГГГ-ММ-ДД, от."},
                "date_to": {"type": "string", "description": "Дата ГГГГ-ММ-ДД, до."},
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "search_text": search_text,
    "filter_reels": filter_reels,
    "get_reel": get_reel,
    "stats": stats,
}
