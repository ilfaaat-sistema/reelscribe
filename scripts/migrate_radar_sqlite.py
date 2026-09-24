#!/usr/bin/env python3
"""Разовый перенос данных Радара из SQLite в Supabase (ТЗ docs/specs/08-radar.md).

Источник — `Радар/backend/data/radar.db` (старый локальный сервис), назначение —
таблицы `radar_*` в Supabase (миграция `supabase/migrations/0008_radar.sql`).
Источник открывается СТРОГО на чтение (`mode=ro`), скрипт в него никогда не пишет.

Порядок переноса (важен из-за внешних ключей):
    competitors → scrape_runs → reels → analyses
`job_queue` и `comment_analyses` вне контракта переноса — не трогаются (см. ТЗ).

Режимы:
    --dry-run (по умолчанию)  — только читает SQLite, печатает сводку, в Supabase НЕ пишет.
    --apply                   — реальная запись (upsert, идемпотентно — повтор не плодит дубли).

Запуск (dry-run, из backend/.venv проекта):
    backend/.venv/bin/python scripts/migrate_radar_sqlite.py
    backend/.venv/bin/python scripts/migrate_radar_sqlite.py --apply

Клиент Supabase (app.core.db) импортируется ТОЛЬКО в ветке --apply — dry-run не требует
.env и не ходит в сеть, ему достаточно stdlib.

После первого --apply id `radar_scrape_runs` переносятся как есть (identity-колонка,
insert с явным id её не двигает) — в конце скрипт печатает готовый SQL для сдвига
счётчика; выполняет его ведущая сессия отдельно, сам скрипт SQL не исполняет.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── пути по умолчанию ────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../worktrees/radar/scripts
_WORKTREE_ROOT = _SCRIPT_DIR.parent                     # .../worktrees/radar
_DEFAULT_SQLITE_PATH = (
    _WORKTREE_ROOT.parent.parent.parent / "Радар" / "backend" / "data" / "radar.db"
)  # .../Рилс парсер и радар/Радар/backend/data/radar.db

_OUT_DIR = _SCRIPT_DIR / ".out"

# Тестовые рилсы старого сервиса — в перенос не идут (см. ТЗ, критерии приёмки).
_TEST_PREFIX = "TEST"

_SCRAPE_RUN_STATUSES = {"pending", "running", "done", "error"}
_ANALYSIS_STATUSES = {
    "queued", "downloading", "analyzing", "rate_limited", "done", "error", "cancelled",
}
_ANALYSIS_MODES = {"t", "v", "tv"}

_CHUNK = 200  # размер пачки upsert — с запасом ниже лимита PostgREST


# ── утилиты ──────────────────────────────────────────────────────────────────

def to_iso(value: Optional[str]) -> Optional[str]:
    """SQLite-строку времени → ISO 8601 с зоной UTC (для timestamptz).

    Источник хранит два формата: 'YYYY-MM-DD HH:MM:SS' (naive, писался
    datetime('now') — то есть UTC) и уже готовый ISO с 'Z' или '+00:00'.
    Оба сводятся к одному виду. Нераспознанное — возвращается как есть,
    пусть Postgres сам пожалуется явной ошибкой, а не проглатывается молча.
    """
    if not value:
        return None
    v = value.strip().replace(" ", "T", 1)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_json_or_none(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_hashtags(value: Optional[str]) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def parse_usernames(value: Optional[str]) -> list[str]:
    """python-repr вида "['directoreels']" → список строк."""
    if not value:
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return [value]
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed]
    return [str(parsed)]


# ── чтение источника ─────────────────────────────────────────────────────────

def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"SQLite-файл не найден: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


# ── трансформации: sqlite-row → payload для upsert ──────────────────────────

def transform_competitor(row: sqlite3.Row) -> dict:
    return {
        "username": row["username"],
        "followers": row["followers"],
        "followers_updated_at": to_iso(row["followers_updated_at"]),
        "added_at": to_iso(row["added_at"]) or datetime.now(timezone.utc).isoformat(),
    }


def transform_scrape_run(row: sqlite3.Row) -> tuple[dict, Optional[str]]:
    """Возвращает (payload, заметка_о_маппинге|None)."""
    status = (row["status"] or "pending").strip()
    error = row["error"]
    note = None
    if status not in _SCRAPE_RUN_STATUSES:
        note = f"статус '{status}' не входит в pending|running|done|error → error"
        error = f"{error} (исходный статус: {status!r})" if error else f"(исходный статус: {status!r})"
        status = "error"
    payload = {
        "id": row["id"],
        "kind": "reels",
        "usernames": parse_usernames(row["usernames"]),
        "period_months": row["period_months"],
        "status": status,
        "apify_run_id": None,
        "apify_dataset_id": None,
        "apify_token_ref": None,
        "results_count": row["results_count"],
        "cost_estimate_usd": row["cost_estimate_usd"],
        "error": error,
        "created_at": to_iso(row["created_at"]),
        "finished_at": None,
    }
    return payload, note


def transform_reel(row: sqlite3.Row, valid_run_ids: set[int]) -> tuple[dict, Optional[str]]:
    note = None
    run_id = row["scrape_run_id"]
    if run_id is not None and run_id not in valid_run_ids:
        note = f"scrape_run_id={run_id} отсутствует среди scrape_runs → NULL"
        run_id = None
    payload = {
        "id": row["id"],
        "username": row["username"],
        "url": row["url"],
        "caption": row["caption"],
        "hashtags": parse_hashtags(row["hashtags"]),
        "music": row["music"],
        "views": row["views"],
        "likes": row["likes"],
        "comments": row["comments"],
        "duration_sec": row["duration_sec"],
        "posted_at": to_iso(row["posted_at"]),
        "video_url": row["video_url"],
        "scrape_run_id": run_id,
        # updated_at не переносим — в источнике такой колонки нет, ставит default now().
    }
    return payload, note


def transform_analysis(row: sqlite3.Row) -> tuple[Optional[dict], Optional[str]]:
    """Возвращает (payload|None, причина_пропуска|None)."""
    mode = row["mode"]
    if mode not in _ANALYSIS_MODES:
        return None, f"mode={mode!r} не входит в t|v|tv — строка пропущена"

    status = (row["status"] or "queued").strip()
    error = row["error"]
    if status not in _ANALYSIS_STATUSES:
        error = f"{error} (исходный статус: {status!r})" if error else f"(исходный статус: {status!r})"
        status = "error"

    payload = {
        "reel_id": row["reel_id"],
        "mode": mode,
        "status": status,
        "transcript_segments": parse_json_or_none(row["transcript_segments"]),
        "transcript_engine": row["transcript_engine"],
        "transcript_language": None,  # нет колонки в источнике
        "visual_timeline": parse_json_or_none(row["visual_timeline"]),
        "hook": row["hook"],
        "structure": row["structure"],
        "cta": row["cta"],
        "format_idea": row["format_idea"],
        "error": error,
        "updated_at": to_iso(row["updated_at"]) or datetime.now(timezone.utc).isoformat(),
    }
    return payload, None


# ── запись в Supabase (только --apply) ───────────────────────────────────────

def get_supabase_client():
    """Ленивый импорт app.core.db — только когда реально нужна запись.

    Повторяет приём из scripts/health.py: backend/ добавляется в sys.path и
    становится cwd ДО импорта app.*, потому что Settings() ищет .env
    относительно текущей директории.
    """
    backend_dir = _WORKTREE_ROOT / "backend"
    sys.path.insert(0, str(backend_dir))
    import os
    os.chdir(backend_dir)
    from app.core.db import get_db  # noqa: E402
    return get_db()


def upsert_chunked(client, table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()


# ── основной сценарий ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=_DEFAULT_SQLITE_PATH,
        help=f"Путь к radar.db (по умолчанию: {_DEFAULT_SQLITE_PATH})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Только чтение и печать сводки (по умолчанию)")
    mode.add_argument("--apply", action="store_true", help="Реальная запись в Supabase (upsert)")
    args = parser.parse_args()
    apply_mode = args.apply  # без --apply всегда dry-run, даже если забыли --dry-run

    print(f"Источник: {args.sqlite_path}")
    print(f"Режим: {'ЗАПИСЬ (--apply)' if apply_mode else 'dry-run (только чтение)'}\n")

    conn = open_readonly(args.sqlite_path)

    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sqlite_path": str(args.sqlite_path),
        "apply": apply_mode,
        "tables": {},
    }

    # ── competitors ──────────────────────────────────────────────────────────
    competitor_rows = fetch_all(conn, "SELECT * FROM competitors")
    competitors_payload = [transform_competitor(r) for r in competitor_rows]
    summary["tables"]["radar_competitors"] = {
        "read": len(competitor_rows),
        "skipped": 0,
        "to_write": len(competitors_payload),
        "notes": [],
    }

    # ── scrape_runs ──────────────────────────────────────────────────────────
    scrape_run_rows = fetch_all(conn, "SELECT * FROM scrape_runs")
    scrape_runs_payload: list[dict] = []
    scrape_run_notes: list[str] = []
    for r in scrape_run_rows:
        payload, note = transform_scrape_run(r)
        scrape_runs_payload.append(payload)
        if note:
            scrape_run_notes.append(f"id={r['id']}: {note}")
    valid_run_ids = {r["id"] for r in scrape_run_rows}
    summary["tables"]["radar_scrape_runs"] = {
        "read": len(scrape_run_rows),
        "skipped": 0,
        "to_write": len(scrape_runs_payload),
        "notes": scrape_run_notes,
    }

    # ── reels (кроме TEST%) ──────────────────────────────────────────────────
    all_reel_rows = fetch_all(conn, "SELECT * FROM reels")
    reel_rows = [r for r in all_reel_rows if not str(r["id"]).startswith(_TEST_PREFIX)]
    skipped_test_reels = [r["id"] for r in all_reel_rows if str(r["id"]).startswith(_TEST_PREFIX)]
    reels_payload: list[dict] = []
    reel_notes: list[str] = list(skipped_test_reels and [f"пропущены тестовые id: {skipped_test_reels}"] or [])
    for r in reel_rows:
        payload, note = transform_reel(r, valid_run_ids)
        reels_payload.append(payload)
        if note:
            reel_notes.append(f"id={r['id']}: {note}")
    summary["tables"]["radar_reels"] = {
        "read": len(all_reel_rows),
        "skipped": len(skipped_test_reels),
        "skipped_reason": f"id LIKE '{_TEST_PREFIX}%'" if skipped_test_reels else None,
        "to_write": len(reels_payload),
        "notes": reel_notes,
    }

    # ── analyses (кроме TEST%) ───────────────────────────────────────────────
    all_analysis_rows = fetch_all(conn, "SELECT * FROM analyses")
    analysis_rows = [r for r in all_analysis_rows if not str(r["reel_id"]).startswith(_TEST_PREFIX)]
    skipped_test_analyses = [r["reel_id"] for r in all_analysis_rows if str(r["reel_id"]).startswith(_TEST_PREFIX)]
    analyses_payload: list[dict] = []
    analysis_notes: list[str] = list(
        skipped_test_analyses and [f"пропущены тестовые reel_id: {skipped_test_analyses}"] or []
    )
    skipped_bad_mode = 0
    for r in analysis_rows:
        payload, skip_reason = transform_analysis(r)
        if payload is None:
            skipped_bad_mode += 1
            analysis_notes.append(f"reel_id={r['reel_id']}: {skip_reason}")
            continue
        analyses_payload.append(payload)
    summary["tables"]["radar_analyses"] = {
        "read": len(all_analysis_rows),
        "skipped": len(skipped_test_analyses) + skipped_bad_mode,
        "skipped_reason": f"id LIKE '{_TEST_PREFIX}%' и/или невалидный mode",
        "to_write": len(analyses_payload),
        "notes": analysis_notes,
    }

    # ── вне контракта переноса — просто считаем, не переносим ──────────────
    for src_table, target_note in (
        ("job_queue", "очередь старого сервиса, у Vercel-версии своя radar_jobs"),
        ("comment_analyses", "фича не реализована в Радаре, таблица пустая"),
    ):
        try:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {src_table}").fetchone()["n"]
        except sqlite3.OperationalError:
            n = 0
        summary["tables"][f"(не переносится) {src_table}"] = {
            "read": n,
            "skipped": n,
            "skipped_reason": f"вне контракта переноса — {target_note}",
            "to_write": 0,
            "notes": [],
        }

    conn.close()

    # ── запись (только --apply) ──────────────────────────────────────────────
    write_errors: list[str] = []
    if apply_mode:
        client = get_supabase_client()
        try:
            upsert_chunked(client, "radar_competitors", competitors_payload, on_conflict="username")
            upsert_chunked(client, "radar_scrape_runs", scrape_runs_payload, on_conflict="id")
            upsert_chunked(client, "radar_reels", reels_payload, on_conflict="id")
            upsert_chunked(client, "radar_analyses", analyses_payload, on_conflict="reel_id")
        except Exception as exc:  # noqa: BLE001 — печатаем и сохраняем в сводку, не глотаем
            write_errors.append(str(exc))
    summary["write_errors"] = write_errors

    # ── сохранить сводку на диск ДО печати ───────────────────────────────────
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _OUT_DIR / f"migrate_radar_{ts}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── печать сводки ─────────────────────────────────────────────────────────
    print("Сводка по таблицам:")
    for table, info in summary["tables"].items():
        line = f"  {table}: прочитано {info['read']}, пропущено {info['skipped']}"
        if info.get("skipped_reason"):
            line += f" ({info['skipped_reason']})"
        line += f", {'записано бы' if not apply_mode else 'записано'} {info['to_write']}"
        print(line)
        for note in info["notes"]:
            print(f"    - {note}")

    if write_errors:
        print("\nОШИБКИ ЗАПИСИ:")
        for e in write_errors:
            print(f"  - {e}")

    setval_sql = (
        "select setval(pg_get_serial_sequence('public.radar_scrape_runs','id'), "
        "(select max(id) from public.radar_scrape_runs));"
    )
    print(
        "\nПосле переноса (когда --apply реально запишет id вручную) выполнить "
        "SQL для сдвига identity-счётчика radar_scrape_runs (сам скрипт его НЕ выполняет):"
    )
    print(f"  {setval_sql}")

    print(f"\nСводка сохранена: {out_path}")
    return 1 if write_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
