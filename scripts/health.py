#!/usr/bin/env python3
"""Единый датчик состояния ReelScribe — «анализы одной командой».

Раньше картину прода собирали вручную командами из `docs/диагностика.md` —
копировали одну за другой в терминал. Этот скрипт делает то же самое одним
прогоном: очередь, дыры в данных, деньги Apify, ключи StarAPI, cooldown и
прогоны воркера. Ничего в пайплайне не меняет — только читает.

Запуск (из КОРНЯ проекта, не из backend/):
    backend/.venv/bin/python scripts/health.py             # без проверки ключей StarAPI
    backend/.venv/bin/python scripts/health.py --starapi    # + живая проверка каждого ключа

Флаг --starapi осторожно: каждая проверка тратит один запрос из месячной сотни
StarAPI НА КАЖДЫЙ ключ, поэтому без флага секция 4 просто считает ключи и
проверку пропускает.

Результат сохраняется в `.health/health-YYYYMMDD-HHMMSS.json` ДО печати сводки
(правило проекта: дорогой прогон сначала на диск, потом в терминал) — путь к
файлу выводится последней строкой.

Падение одной секции (нет сети, нет `gh`, таблица ещё не создана) не роняет
остальные — она помечается недоступной, отчёт печатается целиком. Код выхода
не нулевой ТОЛЬКО если не удалось подключиться к самой базе.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ── backend/ в sys.path и в cwd ДО импорта app.* ────────────────────────────
# app.core.config.Settings ищет .env файл относительно ТЕКУЩЕЙ рабочей
# директории (env_file=".env" — путь относительный), а секреты лежат в
# backend/.env. Скрипт запускается из корня проекта, поэтому переходим в
# backend/ сами, ДО импорта settings — иначе Settings() упадёт валидацией
# (не найдёт supabase_url/supabase_anon_key) или молча возьмёт пустые значения.
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_BACKEND))
os.chdir(_BACKEND)

from app.core.config import settings  # noqa: E402
from app.core.db import get_db  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("health")

_HEALTH_DIR = _ROOT / ".health"
_PAGE = 1000  # PostgREST режет ответ на 1000 строк — тянем очередь постранично
# Ориентир цены за рилс из docs/диагностика.md (§3): $0.0027 одиночный актор +
# $0.0026 profile-актор ≈ $0.0075/рилс суммарно. Только для грубой прикидки
# «на сколько рилсов хватит остатка» в вердикте — не тарифная гарантия.
_APIFY_COST_PER_REEL_USD = 0.0075
# Известный существующий рилс для probe-запроса StarAPI (см. docs/диагностика.md §4).
_STARAPI_PROBE_SHORTCODE = "DRnVrkNAMtn"


# ── утилиты ──────────────────────────────────────────────────────────────────

def _fetch_all(
    db: Any,
    table: str,
    columns: str,
    build: Optional[Callable[[Any], Any]] = None,
) -> list:
    """Постранично тянет все строки таблицы — PostgREST режет ответ на 1000 строк."""
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


def _safe_section(name: str, fn: Callable[[], dict], *args: Any) -> dict:
    """Оборачивает сбор одной секции: падение не должно ронять весь отчёт."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий перехват на границе секции
        logger.warning("Секция «%s» не собралась: %s", name, exc)
        return {"ошибка": f"{type(exc).__name__}: {exc}"}


def _fmt_counts(counts: dict) -> str:
    if not counts:
        return "пусто"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


# ── 1. Очередь ───────────────────────────────────────────────────────────────

def collect_queue(db: Any) -> dict:
    jobs_rows = _fetch_all(db, "jobs", "state")
    jobs_counts = dict(Counter((r.get("state") or "—") for r in jobs_rows))
    tr_rows = _fetch_all(db, "transcripts", "status")
    tr_counts = dict(Counter((r.get("status") or "—") for r in tr_rows))
    return {
        "jobs_всего": len(jobs_rows),
        "jobs_по_состояниям": jobs_counts,
        "transcripts_всего": len(tr_rows),
        "transcripts_по_статусам": tr_counts,
    }


# ── 2. Дыры в данных ─────────────────────────────────────────────────────────

def collect_gaps(db: Any) -> dict:
    # a) queued/failed расшифровки без записи в jobs — воркер такие рилсы не
    # видит вообще (он берёт работу из jobs, не из transcripts). Реальная
    # ловушка проекта, см. docs/диагностика.md §1.
    tr_pending = _fetch_all(
        db, "transcripts", "reel_id,status",
        build=lambda q: q.in_("status", ["queued", "failed"]),
    )
    pending_reel_ids = {r["reel_id"] for r in tr_pending if r.get("reel_id")}
    job_rows = _fetch_all(db, "jobs", "reel_id")
    reel_ids_with_job = {r["reel_id"] for r in job_rows if r.get("reel_id")}
    missing_ids = pending_reel_ids - reel_ids_with_job

    examples: list = []
    if missing_ids:
        sample_ids = list(missing_ids)[:5]
        sample = db.table("reels").select("shortcode").in_("id", sample_ids).execute().data
        examples = [r["shortcode"] for r in sample]

    # b) известен автор, но не подтянуты подписчики
    r_followers = (
        db.table("reels").select("id", count="exact")
        .not_.is_("author_handle", "null")
        .is_("author_followers", "null")
        .limit(1).execute()
    )

    # c) без просмотров
    r_views = db.table("reels").select("id", count="exact").is_("views", "null").limit(1).execute()

    # d) готовая иностранная расшифровка без перевода на русский
    r_untranslated = (
        db.table("transcripts").select("id", count="exact")
        .eq("status", "done")
        .not_.is_("language", "null")
        .neq("language", "ru")
        .or_("text_ru.is.null,text_ru.eq.")
        .limit(1).execute()
    )

    return {
        "без_джобы_но_в_очереди": {
            "количество": len(missing_ids),
            "примеры_shortcode": examples,
        },
        "без_подписчиков_при_известном_авторе": r_followers.count or 0,
        "без_просмотров": r_views.count or 0,
        "готовые_иностранные_без_перевода": r_untranslated.count or 0,
    }


# ── 3. Деньги Apify ──────────────────────────────────────────────────────────

def collect_apify() -> dict:
    # Переиспользуем готовый код опроса Apify из app/workers/apify_quota.py —
    # не копируем логику (ротация /usage/monthly vs /limits, обработку ошибок
    # одного токена), а зовём саму функцию: она уже гарантирует, что токен
    # никогда не попадёт в вывод.
    from app.workers.apify_quota import _check_token, total_spent_usd

    tokens = settings.apify_token_list
    if not tokens:
        return {
            "настроено": False,
            "сообщение": "Apify-токены не заданы (APIFY_API_TOKEN / APIFY_API_TOKENS пусты)",
        }

    per_token = []
    total_remaining = 0.0
    for i, token in enumerate(tokens, 1):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                remaining = _check_token(i, token)
        except Exception as exc:  # noqa: BLE001 — один неответивший токен не должен рушить секцию
            per_token.append({"токен": f"#{i}", "ошибка": f"{type(exc).__name__}: {exc}"})
            continue
        total_remaining += remaining
        per_token.append({
            "токен": f"#{i}",
            "остаток_usd": remaining,
            "детали": buf.getvalue().strip(),
        })

    return {
        "настроено": True,
        "токенов": len(tokens),
        "по_токенам": per_token,
        "остаток_всего_usd": round(total_remaining, 2),
        "потрачено_всего_usd": total_spent_usd(),
    }


# ── 4. Ключи StarAPI ─────────────────────────────────────────────────────────

def collect_starapi(check: bool) -> dict:
    keys = settings.rapidapi_key_list
    if not check:
        return {
            "ключей_в_ротации": len(keys),
            "проверено": False,
            "сообщение": "проверка пропущена (нужен флаг --starapi) — экономим квоту: "
            "каждая проверка тратит запрос из месячной сотни на КАЖДЫЙ ключ",
        }
    if not keys:
        return {"ключей_в_ротации": 0, "проверено": True, "ключи": []}

    import httpx

    entries = []
    for i, key in enumerate(keys, 1):
        entry: dict = {"ключ": f"#{i} …{key[-6:]}"}
        try:
            resp = httpx.post(
                f"https://{settings.starapi_host}/instagram/media/get_info_by_shortcode",
                headers={
                    "x-rapidapi-key": key,
                    "x-rapidapi-host": settings.starapi_host,
                    "Content-Type": "application/json",
                },
                json={"shortcode": _STARAPI_PROBE_SHORTCODE},
                timeout=45,
            )
            entry["http_статус"] = resp.status_code
            entry["остаток"] = resp.headers.get("x-ratelimit-requests-remaining", "?")
            entry["лимит"] = resp.headers.get("x-ratelimit-requests-limit", "?")
            if resp.status_code == 429:
                entry["состояние"] = "месячная квота исчерпана"
            elif resp.status_code == 403:
                entry["состояние"] = "ключ жив, но нет подписки на StarAPI"
            elif resp.status_code == 200:
                entry["состояние"] = "ок"
            else:
                entry["состояние"] = f"неожиданный статус {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — один недоступный ключ не должен рушить секцию
            entry["ошибка"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        entries.append(entry)

    return {"ключей_в_ротации": len(keys), "проверено": True, "ключи": entries}


# ── 5. Cooldown ключей ───────────────────────────────────────────────────────

def collect_cooldown(db: Any) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = (
        db.table("key_cooldown").select("provider,actor,key_ref,until")
        .gt("until", now_iso)
        .order("until", desc=True)
        .execute().data
    )
    return {"активных_записей": len(rows), "записи": rows}


# ── 6. Прогоны воркера ───────────────────────────────────────────────────────

def collect_worker_runs(db: Any) -> dict:
    result: dict = {}

    if shutil.which("gh") is None:
        result["gh"] = {"доступен": False, "сообщение": "gh недоступен"}
    else:
        try:
            proc = subprocess.run(
                [
                    "gh", "run", "list",
                    "--workflow=worker.yml",
                    "--repo", settings.github_dispatch_repo,
                    "--json", "databaseId,status,conclusion,createdAt,updatedAt,displayTitle",
                    "-L", "5",
                ],
                capture_output=True, text=True, timeout=30, check=True,
            )
            result["gh"] = {"доступен": True, "прогоны": json.loads(proc.stdout)}
        except Exception as exc:  # noqa: BLE001 — gh недоступен/не авторизован — не роняем отчёт
            result["gh"] = {"доступен": False, "ошибка": f"{type(exc).__name__}: {str(exc)[:200]}"}

    try:
        rows = (
            db.table("worker_runs").select("*")
            .order("started_at", desc=True)
            .limit(5).execute().data
        )
        result["таблица"] = {"существует": True, "прогоны": rows}
    except Exception:  # noqa: BLE001 — таблицы ещё нет (миграция 0004 не применена) — это ОК
        result["таблица"] = {
            "существует": False,
            "сообщение": "таблица worker_runs ещё не создана (миграция 0004_observability.sql не применена)",
        }

    return result


# ── 7. Вердикт ───────────────────────────────────────────────────────────────

def build_verdict(report: dict) -> list:
    warnings: list = []
    info: list = []

    queue = report.get("queue", {})
    if "ошибка" in queue:
        warnings.append(f"⚠ секция «очередь» не собралась — {queue['ошибка']}")
    else:
        jc = queue.get("jobs_по_состояниям", {})
        queued, in_progress = jc.get("queued", 0), jc.get("in_progress", 0)
        if queued or in_progress:
            info.append(
                f"Очередь разбирается: queued={queued}, in_progress={in_progress}, "
                f"done={jc.get('done', 0)}, failed={jc.get('failed', 0)}."
            )
        else:
            info.append("Очередь пуста — новых заданий на разбор нет.")

    gaps = report.get("gaps", {})
    if "ошибка" in gaps:
        warnings.append(f"⚠ секция «дыры в данных» не собралась — {gaps['ошибка']}")
    else:
        missing = gaps.get("без_джобы_но_в_очереди", {}).get("количество", 0)
        if missing:
            warnings.append(f"⚠ {missing} рилсов не видны воркеру — нет записи в jobs.")
        no_followers = gaps.get("без_подписчиков_при_известном_авторе", 0)
        no_views = gaps.get("без_просмотров", 0)
        if no_followers or no_views:
            info.append(f"В базе {no_followers} рилсов без подписчиков автора, {no_views} без просмотров.")
        untranslated = gaps.get("готовые_иностранные_без_перевода", 0)
        if untranslated:
            info.append(f"{untranslated} готовых иностранных расшифровок без перевода на русский.")

    apify = report.get("apify", {})
    if "ошибка" in apify:
        warnings.append(f"⚠ секция «деньги Apify» не собралась — {apify['ошибка']}")
    elif apify.get("настроено"):
        remaining = apify.get("остаток_всего_usd")
        if remaining is not None:
            est_reels = max(int(remaining / _APIFY_COST_PER_REEL_USD), 0)
            line = f"Apify: остаток ${remaining:.2f} по {apify.get('токенов')} токенам (~{est_reels} рилсов по ориентиру ${_APIFY_COST_PER_REEL_USD}/рилс)."
            if remaining < 1:
                warnings.append("⚠ " + line)
            else:
                info.append(line)

    cooldown = report.get("cooldown", {})
    if "ошибка" in cooldown:
        warnings.append(f"⚠ секция «cooldown» не собралась — {cooldown['ошибка']}")
    elif cooldown.get("активных_записей"):
        info.append(f"{cooldown['активных_записей']} ключей сейчас на cooldown.")
    else:
        info.append("Cooldown пуст — ни один ключ сейчас не остывает.")

    lines = warnings + info
    if not lines:
        lines = ["Данных для вердикта недостаточно — все секции недоступны."]
    return lines[:5]


# ── рендер в терминал ────────────────────────────────────────────────────────

def render(report: dict) -> str:
    out: list = []
    out.append("=" * 78)
    out.append(f"ReelScribe — состояние сервиса на {report['собрано_в']}")
    out.append("=" * 78)

    out.append("\n1. ОЧЕРЕДЬ")
    q = report["queue"]
    if "ошибка" in q:
        out.append(f"  недоступно: {q['ошибка']}")
    else:
        out.append(f"  jobs (всего {q['jobs_всего']}): {_fmt_counts(q['jobs_по_состояниям'])}")
        out.append(f"  transcripts (всего {q['transcripts_всего']}): {_fmt_counts(q['transcripts_по_статусам'])}")

    out.append("\n2. ДЫРЫ В ДАННЫХ")
    g = report["gaps"]
    if "ошибка" in g:
        out.append(f"  недоступно: {g['ошибка']}")
    else:
        hole = g["без_джобы_но_в_очереди"]
        out.append(f"  без записи в jobs, но queued/failed: {hole['количество']}" + (
            f" (примеры: {', '.join(hole['примеры_shortcode'])})" if hole["примеры_shortcode"] else ""
        ))
        out.append(f"  без author_followers при известном авторе: {g['без_подписчиков_при_известном_авторе']}")
        out.append(f"  без views: {g['без_просмотров']}")
        out.append(f"  готовые иностранные без перевода на русский: {g['готовые_иностранные_без_перевода']}")

    out.append("\n3. ДЕНЬГИ APIFY")
    a = report["apify"]
    if "ошибка" in a:
        out.append(f"  недоступно: {a['ошибка']}")
    elif not a.get("настроено"):
        out.append(f"  {a['сообщение']}")
    else:
        for t in a["по_токенам"]:
            if "ошибка" in t:
                out.append(f"  {t['токен']}: ошибка — {t['ошибка']}")
            else:
                out.append(f"  {t['детали']}")
        out.append(f"  Остаток всего: ${a['остаток_всего_usd']:.2f} ({a['токенов']} токен(ов))")
        spent = a.get("потрачено_всего_usd")
        out.append(f"  Потрачено за месяц (total_spent_usd): {'$' + format(spent, '.4f') if spent is not None else 'недоступно'}")

    out.append("\n4. КЛЮЧИ STARAPI")
    s = report["starapi"]
    if "ошибка" in s:
        out.append(f"  недоступно: {s['ошибка']}")
    elif not s.get("проверено"):
        out.append(f"  ключей в ротации: {s['ключей_в_ротации']} — {s['сообщение']}")
    else:
        out.append(f"  ключей в ротации: {s['ключей_в_ротации']}")
        for k in s["ключи"]:
            if "ошибка" in k:
                out.append(f"  {k['ключ']}: ошибка — {k['ошибка']}")
            else:
                out.append(
                    f"  {k['ключ']}: HTTP {k['http_статус']} — {k['состояние']} "
                    f"(остаток {k['остаток']} из {k['лимит']})"
                )

    out.append("\n5. COOLDOWN КЛЮЧЕЙ")
    c = report["cooldown"]
    if "ошибка" in c:
        out.append(f"  недоступно: {c['ошибка']}")
    elif not c["активных_записей"]:
        out.append("  пусто — ни один ключ сейчас не остывает (это нормально)")
    else:
        for rec in c["записи"]:
            out.append(f"  {rec['provider']} …{rec['key_ref'][-6:]} (actor={rec['actor'] or '—'}) до {rec['until']}")

    out.append("\n6. ПРОГОНЫ ВОРКЕРА")
    w = report["worker_runs"]
    if "ошибка" in w:
        out.append(f"  недоступно: {w['ошибка']}")
    else:
        gh = w.get("gh", {})
        if not gh.get("доступен"):
            out.append(f"  gh: {gh.get('сообщение') or gh.get('ошибка')}")
        else:
            out.append("  последние прогоны на GitHub Actions (gh run list):")
            for run in gh["прогоны"]:
                out.append(
                    f"    #{run['databaseId']} {run['status']}"
                    + (f"/{run['conclusion']}" if run.get("conclusion") else "")
                    + f" начат {run['createdAt']}, обновлён {run['updatedAt']}"
                )
        table = w.get("таблица", {})
        if not table.get("существует"):
            out.append(f"  таблица worker_runs: {table.get('сообщение')}")
        else:
            out.append("  последние записи из public.worker_runs:")
            for rec in table["прогоны"]:
                out.append(
                    f"    {rec.get('started_at')} outcome={rec.get('outcome') or 'идёт'} "
                    f"done={rec.get('jobs_done')} no_audio={rec.get('jobs_no_audio')} "
                    f"failed={rec.get('jobs_failed')} apify=${rec.get('apify_spent_usd')}"
                )

    out.append("\n7. ВЕРДИКТ")
    for line in report["вердикт"]:
        out.append(f"  {line}")

    return "\n".join(out)


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Единый датчик состояния ReelScribe")
    parser.add_argument(
        "--starapi", action="store_true",
        help="живая проверка каждого ключа StarAPI (тратит запрос из месячной сотни на КАЖДЫЙ ключ)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        db = get_db()
        db.table("jobs").select("id").limit(1).execute()
    except Exception as exc:  # noqa: BLE001 — единственный случай, когда отчёт вообще не имеет смысла
        print(f"ФАТАЛЬНО: не удалось подключиться к базе — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report: dict = {
        "собрано_в": datetime.now(timezone.utc).isoformat(),
        "starapi_проверен": args.starapi,
    }
    report["queue"] = _safe_section("очередь", collect_queue, db)
    report["gaps"] = _safe_section("дыры в данных", collect_gaps, db)
    report["apify"] = _safe_section("деньги Apify", collect_apify)
    report["starapi"] = _safe_section("ключи StarAPI", collect_starapi, args.starapi)
    report["cooldown"] = _safe_section("cooldown", collect_cooldown, db)
    report["worker_runs"] = _safe_section("прогоны воркера", collect_worker_runs, db)
    report["вердикт"] = build_verdict(report)

    _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = _HEALTH_DIR / f"health-{ts}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(render(report))
    print(f"\nJSON сохранён: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
