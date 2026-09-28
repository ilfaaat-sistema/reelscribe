"""Инструменты ИИ-чата (ТЗ 09) — читают базу и возвращают компактный JSON модели.

Поиск идёт на стороне Postgres через PostgREST-фильтр `imatch` (регистронезависимый POSIX
regex, `column=imatch.pattern`) — строки в Python НЕ выкачиваются целиком, фильтрация в базе.
`imatch` — метод отсутствует в билдере postgrest-py явным именем, но есть универсальный
`.filter(column, operator, value)`, который собирает `column=imatch.pattern` (проверено
09-2026: `postgrest._sync.request_builder.SyncFilterRequestBuilder.filter` кладёт
`f"{operator}.{criteria}"` как есть — оператор PostgREST может быть любым, включая `imatch`).

`search_text` умеет ещё и фильтровать по дате/просмотрам/залётности и сортировать (доработка
после живого прогона: модель отказывалась совмещать текстовый поиск с фильтром по дате).
Для полей `transcripts.*` фильтр по данным рилса (`posted_at`/`views`/`er`) идёт через embed
`reels!inner(...)` — обычный `reels(...)` только обнуляет вложенный объект у несовпавших строк,
а `!inner` превращает embed в JOIN и реально отсекает строки на стороне Postgres (проверено
09-2026 на живой базе: без `!inner` фильтр по `posted_at` не менял число строк, с `!inner` —
менял). У `radar_reels` нет подписчиков и, соответственно, залётности — при `min_er` радар
целиком исключается из поиска, а не просто не подходит под фильтр.

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

_REELS_META_COLS = "id,shortcode,url,author_handle,views,likes,er,posted_at"

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


SNIPPET_WORD_SNAP_MAX = 60  # не откатываемся к границе слова дальше, чем на столько символов


def _word_boundary_start(text: str, idx: int) -> int:
    """Сдвигает левую границу сниппета назад, чтобы не резать слово посередине."""
    if idx <= 0:
        return 0
    if idx >= len(text) or text[idx - 1].isspace():
        return idx
    limit = max(0, idx - SNIPPET_WORD_SNAP_MAX)
    j = idx
    while j > limit and not text[j - 1].isspace():
        j -= 1
    return j


def _word_boundary_end(text: str, idx: int) -> int:
    """Сдвигает правую границу сниппета вперёд, чтобы не резать слово посередине."""
    n = len(text)
    if idx >= n:
        return n
    if idx <= 0 or text[idx].isspace():
        return idx
    limit = min(n, idx + SNIPPET_WORD_SNAP_MAX)
    j = idx
    while j < limit and not text[j].isspace():
        j += 1
    return j


def _make_snippet(text: str | None, pattern: str) -> str | None:
    """Вырезка вокруг совпадения, обрезанная по границам слов, с «…» по краям при обрезке."""
    if not text:
        return None
    try:
        m = re.search(pattern, text, re.IGNORECASE)
    except re.error:
        m = None
    if m:
        raw_start = max(0, m.start() - SNIPPET_RADIUS)
        raw_end = min(len(text), m.end() + SNIPPET_RADIUS)
    else:
        # Регекс на стороне Postgres мог найти совпадение (иной диалект regex), а Python re —
        # нет. В этом случае просто отдаём начало текста, лишь бы не падать.
        raw_start = 0
        raw_end = min(len(text), SNIPPET_RADIUS * 2)

    start = _word_boundary_start(text, raw_start)
    end = _word_boundary_end(text, raw_end)
    fragment = text[start:end].strip()
    if not fragment:
        return None
    if start > 0:
        fragment = "…" + fragment
    if end < len(text):
        fragment = fragment + "…"
    return fragment


_SORT_FIELDS = ("views", "er", "posted_at", "likes")


def _apply_reels_range_filters(
    q: Any,
    prefix: str,
    date_from: str | None,
    date_to: str | None,
    min_views: int | None,
    min_er: float | None,
) -> Any:
    """Фильтры по дате/просмотрам/залётности. `prefix` — "" для таблицы reels напрямую,

    "reels." — для embed-фильтра поверх `reels!inner(...)` (нужен PostgREST embed-синтаксис
    `<embed>.<column>=<op>.<value>`, а не обычное имя колонки).
    """
    if date_from:
        q = q.gte(f"{prefix}posted_at", date_from)
    if date_to:
        q = q.lte(f"{prefix}posted_at", date_to)
    if min_views is not None:
        q = q.gte(f"{prefix}views", min_views)
    if min_er is not None:
        q = q.gte(f"{prefix}er", min_er)
    return q


def _dedup_by_source_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один рилс, найденный через несколько полей, — одна запись (сниппет из первого поля).

    Записи-ошибки (`{"error": ...}`, без source/id) дедупу не подлежат — проходят все как есть.
    """
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("source") is None or item.get("id") is None:
            out.append(item)  # ошибка отдельного field — не карточка рилса
            continue
        key = (item["source"], item["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _sort_results(items: list[dict[str, Any]], sort_by: str | None) -> list[dict[str, Any]]:
    """Сортировка по убыванию после объединения и дедупа. Без sort_by — порядок как пришёл.

    Записи без значения нужного поля (например `er=None` у радара) уходят в конец, а не роняют
    сортировку сравнением `None` с числом. Записи-ошибки — в самый конец, их не сортируем.
    """
    if not sort_by:
        return items
    errors, plain = [], []
    for item in items:
        if item.get("source") is None or item.get("id") is None:
            errors.append(item)
        else:
            plain.append(item)
    with_value = [it for it in plain if it.get(sort_by) is not None]
    without_value = [it for it in plain if it.get(sort_by) is None]
    with_value.sort(key=lambda it: it[sort_by], reverse=True)
    return with_value + without_value + errors


def search_text(
    pattern: str,
    fields: list[str] | None = None,
    limit: int = 20,
    date_from: str | None = None,
    date_to: str | None = None,
    min_views: int | None = None,
    min_er: float | None = None,
    sort_by: str | None = None,
) -> list[dict[str, Any]]:
    """Регулярное выражение POSIX без учёта регистра по полям reels/transcripts/radar_reels.

    Поиск целиком на стороне Postgres (PostgREST `imatch`), фильтры по дате/просмотрам/залётности
    — тоже в PostgREST (embed `reels!inner(...)` для полей transcripts). В Python — только
    вырезка сниппета, дедуп одного рилса, найденного через несколько полей, и сортировка (её
    Postgres сделать не может, т.к. итог собирается из нескольких независимых запросов).
    `min_er` исключает `radar_reels` целиком — у радара нет подписчиков, значит и залётности.
    """
    db = get_db()
    limit = max(1, min(limit, 50))
    if sort_by not in (None, *_SORT_FIELDS):
        sort_by = None

    field_names = list(_FIELD_SPECS.keys())
    if fields:
        resolved = [_resolve_field(f) for f in fields]
        field_names = [f for f in resolved if f]
        if not field_names:
            field_names = list(_FIELD_SPECS.keys())

    has_reels_filter = bool(date_from or date_to or min_views is not None or min_er is not None)

    results: list[dict[str, Any]] = []
    for field_name in field_names:
        spec = _FIELD_SPECS[field_name]
        try:
            if spec["kind"] == "reels":
                q = (
                    db.table("reels")
                    .select(f"{_REELS_META_COLS},{spec['column']}")
                    .filter(spec["column"], "imatch", pattern)
                )
                q = _apply_reels_range_filters(q, "", date_from, date_to, min_views, min_er)
                resp = q.limit(limit).execute()
                for row in resp.data or []:
                    results.append(
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
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
            elif spec["kind"] == "transcripts":
                # !inner только когда реально нужен фильтр по полям рилса — обычный embed
                # (без !inner) лишь обнуляет вложенный reels у несовпавших строк, а не убирает
                # их: строка transcripts всё равно осталась бы в ответе с пустым reels.
                reels_part = (
                    f"reels!inner({_REELS_META_COLS})" if has_reels_filter
                    else f"reels({_REELS_META_COLS})"
                )
                q = (
                    db.table("transcripts")
                    .select(f"reel_id,{spec['column']},{reels_part}")
                    .filter(spec["column"], "imatch", pattern)
                )
                q = _apply_reels_range_filters(
                    q, "reels.", date_from, date_to, min_views, min_er
                )
                resp = q.limit(limit).execute()
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
                            "likes": reel.get("likes"),
                            "er": reel.get("er"),
                            "posted_at": reel.get("posted_at"),
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
            else:  # radar — своей залётности нет; при min_er исключаем весь источник
                if min_er is not None:
                    continue
                q = (
                    db.table("radar_reels")
                    .select("id,username,url,views,likes,posted_at,caption")
                    .filter(spec["column"], "imatch", pattern)
                )
                if date_from:
                    q = q.gte("posted_at", date_from)
                if date_to:
                    q = q.lte("posted_at", date_to)
                if min_views is not None:
                    q = q.gte("views", min_views)
                resp = q.limit(limit).execute()
                for row in resp.data or []:
                    results.append(
                        {
                            "source": "radar",
                            "id": row.get("id"),
                            "shortcode": row.get("id"),
                            "url": row.get("url"),
                            "author": row.get("username"),
                            "views": row.get("views"),
                            "likes": row.get("likes"),
                            "er": None,
                            "posted_at": row.get("posted_at"),
                            "field": field_name,
                            "snippet": _make_snippet(row.get(spec["column"]), pattern),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 — один сбойный field не должен ронять весь поиск
            results.append({"error": f"поиск по {field_name} не удался: {exc}"})

    deduped = _dedup_by_source_id(results)
    ordered = _sort_results(deduped, sort_by)
    return ordered[:limit]


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
            "herm[eè]s|гермес|эрмес|хермес. Умеет ОДНОВРЕМЕННО с текстовым поиском фильтровать "
            "по дате/просмотрам/залётности и сортировать — не нужно звать отдельный инструмент "
            "и не нужно отказываться, если в вопросе есть и тема, и период, и «топ по залётности»."
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
                "date_from": {
                    "type": "string",
                    "description": "Дата ГГГГ-ММ-ДД, от (по дате публикации рилса).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Дата ГГГГ-ММ-ДД, до (по дате публикации рилса).",
                },
                "min_views": {
                    "type": "integer",
                    "description": "Минимум просмотров.",
                },
                "min_er": {
                    "type": "number",
                    "description": (
                        "Минимальная залётность (просмотры ÷ подписчики). У рилсов Радара "
                        "залётность не считается — при этом фильтре они не попадают в выдачу."
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["views", "er", "posted_at", "likes"],
                    "description": (
                        "По чему сортировать результат по убыванию. Без указания — порядок как "
                        "нашлось, без сортировки."
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
