#!/usr/bin/env python3
"""Разовая чистка мусорных переводов (transcripts.text_ru, reels.caption_ru).

Причина: до перехода на DeepL (23.09.2026) `_google()` при сбое или пустом ответе клал в
результат ИСХОДНЫЙ текст, а Google иногда отдавал HTML-страницу-заглушку об ошибке как
«успешный» перевод — ни то ни другое не проверялось перед записью в БД. На живой базе это
дало 246 переводов расшифровок с текстом ошибки, 59 дословно равных оригиналу и 458 (из 458)
переводов подписей, дословно равных оригиналу.

Использует ТУ ЖЕ проверку пригодности, что и пайплайн (app.pipeline.translate.
translation_failure_reason). В отличие от пайплайна, здесь категория `unchanged` тоже
считается браком — на живых цифрах это оказался систематический брак (100% подписей), а не
пограничный случай.

Наивная проверка «text_ru дословно равен text» находит 59 расшифровок, но
translation_failure_reason гасит 10 из них через needs_translation-гейт: это короткие фразы
("Oh", "Before. After.") и уже русский текст, ошибочно распознанный как оригинал
("Бургер", "СПОКОЙНАЯ МУЗЫКА") — их обнулять не нужно, это не брак перевода. Проверено на
живой базе 23.09.2026.

Оригиналы (transcripts.text, reels.caption) НЕ трогаются никогда — обнуляется только
text_ru/caption_ru, чтобы запись переехала в очередь на повторный перевод (через
backfill_translate/backfill_caption после появления DEEPL_API_KEY).

Запуск (из КОРНЯ проекта, не из backend/):
    python scripts/clean_garbage_translations.py             # только отчёт, без записи
    python scripts/clean_garbage_translations.py --apply     # обнулить найденный брак
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# ── backend/ в sys.path и в cwd ДО импорта app.* — см. scripts/health.py, та же причина:
# Settings ищет .env относительно ТЕКУЩЕЙ рабочей директории (backend/.env).
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_BACKEND))
import os  # noqa: E402

os.chdir(_BACKEND)

from app.core.db import get_db  # noqa: E402
from app.pipeline.translate import translation_failure_reason  # noqa: E402

_PAGE = 1000  # PostgREST режет ответ на 1000 строк — тянем постранично (грабли 23.09.2026)
_UPDATE_BATCH = 200  # сколько строк обнулять между строками прогресса в логе
_EXAMPLES_PER_REASON = 5


def _fetch_all(db: Any, table: str, columns: str, build: Optional[Callable[[Any], Any]] = None) -> list:
    rows: list = []
    start = 0
    while True:
        q = db.table(table).select(columns)
        if build is not None:
            q = build(q)
        q = q.range(start, start + _PAGE - 1)
        chunk = q.execute().data or []
        rows.extend(chunk)
        if len(chunk) < _PAGE:
            break
        start += _PAGE
    return rows


def _classify(rows: list[dict], original_field: str, translated_field: str) -> dict:
    """Раскладывает строки по причине брака. `unchanged` здесь тоже брак (см. докстринг)."""
    by_reason: dict[str, list[dict]] = {}
    for row in rows:
        reason = translation_failure_reason(row.get(original_field), row.get(translated_field))
        if reason is None:
            continue
        by_reason.setdefault(reason, []).append(row)
    return by_reason


def _fetch_transcripts(db: Any) -> list[dict]:
    """transcripts с непустым text_ru, плюс shortcode рилса — только для отчёта/логов."""
    rows = _fetch_all(
        db, "transcripts", "id,reel_id,text,text_ru",
        build=lambda q: q.not_.is_("text_ru", "null").neq("text_ru", ""),
    )
    # Батч для .in_() гораздо меньше _PAGE: сотни UUID в одном запросе раздувают query-string
    # до 400 Bad Request на стороне PostgREST (поймано на живой базе 23.09.2026).
    _IN_BATCH = 100
    reel_ids = [r["reel_id"] for r in rows if r.get("reel_id")]
    shortcode_by_id: dict = {}
    for i in range(0, len(reel_ids), _IN_BATCH):
        batch = reel_ids[i:i + _IN_BATCH]
        if not batch:
            continue
        chunk = db.table("reels").select("id,shortcode").in_("id", batch).execute().data or []
        shortcode_by_id.update({r["id"]: r["shortcode"] for r in chunk})
    for r in rows:
        r["shortcode"] = shortcode_by_id.get(r.get("reel_id"))
    return rows


def _fetch_reels_with_caption(db: Any) -> list[dict]:
    return _fetch_all(
        db, "reels", "id,shortcode,caption,caption_ru",
        build=lambda q: q.not_.is_("caption_ru", "null").neq("caption_ru", ""),
    )


def _print_report(title: str, by_reason: dict[str, list[dict]], total_checked: int) -> None:
    total_bad = sum(len(v) for v in by_reason.values())
    print(f"\n{title}: проверено {total_checked}, брака {total_bad}")
    for reason, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        shortcodes = [r.get("shortcode") for r in rows[:_EXAMPLES_PER_REASON]]
        print(f"  {reason}: {len(rows)} (примеры: {', '.join(str(s) for s in shortcodes)})")


def _apply_updates(db: Any, table: str, id_field: str, clear_field: str, rows: list[dict]) -> None:
    total = len(rows)
    for i, row in enumerate(rows, 1):
        db.table(table).update({clear_field: None}).eq("id", row[id_field]).execute()
        if i % _UPDATE_BATCH == 0 or i == total:
            print(f"  {table}.{clear_field}: обнулено {i}/{total}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Найти и (опционально) обнулить мусорные переводы.")
    parser.add_argument(
        "--apply", action="store_true",
        help="обнулить найденный брак (без флага — только отчёт, ничего не меняет)",
    )
    args = parser.parse_args()

    db = get_db()

    transcripts = _fetch_transcripts(db)
    tr_bad = _classify(transcripts, "text", "text_ru")
    _print_report("Расшифровки (transcripts.text_ru)", tr_bad, len(transcripts))

    reels = _fetch_reels_with_caption(db)
    reel_bad = _classify(reels, "caption", "caption_ru")
    _print_report("Подписи (reels.caption_ru)", reel_bad, len(reels))

    tr_bad_rows = [r for rows in tr_bad.values() for r in rows]
    reel_bad_rows = [r for rows in reel_bad.values() for r in rows]

    print(f"\nИтого к обнулению: {len(tr_bad_rows)} расшифровок, {len(reel_bad_rows)} подписей.")
    print("Оригиналы (transcripts.text, reels.caption) не трогаются.")

    if not args.apply:
        print("\nБез --apply: ничего не изменено. Запусти с --apply, чтобы обнулить найденный брак.")
        return 0

    print("\nОбнуляю…")
    _apply_updates(db, "transcripts", "id", "text_ru", tr_bad_rows)
    _apply_updates(db, "reels", "id", "caption_ru", reel_bad_rows)
    print("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
