#!/usr/bin/env python3
"""Разведка Instagram Business Discovery — официальный источник метрик вместо Apify/StarAPI.

Зачем: сейчас ReelScribe платит за метрики и медиа (Apify ~$0.0027 за рилс, StarAPI по
лимитам ключей). Business Discovery отдаёт то же самое бесплатно по токену Meta:
подписчиков, просмотры рилса (`view_count` — доступен ТОЛЬКО через этот эндпоинт),
лайки, комментарии, подпись и прямую ссылку на медиа.

Этот скрипт ничего не меняет в пайплайне — только спрашивает Graph API и показывает,
что реально приходит по конкретному блогеру. Нужен, чтобы решить, переводить ли на него
пайплайн, ДО того как писать модуль.

Запуск (из КОРНЯ проекта):
    backend/.venv/bin/python scripts/probe_business_discovery.py <ник_без_собаки>
    backend/.venv/bin/python scripts/probe_business_discovery.py maximzheleznov --limit 25

Читает из backend/.env:
    META_IG_TOKEN    — маркер системного пользователя (в вывод НЕ печатается)
    META_IG_USER_ID  — ID твоего IG Business аккаунта (от его имени идёт запрос)

Результат сохраняется в `.probe/bd-<ник>-YYYYMMDD-HHMMSS.json` ДО печати сводки
(правило проекта: дорогой прогон сначала на диск) — путь выводится последней строкой.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "backend" / ".env"
OUT_DIR = ROOT / ".probe"

# Поля медиа. view_count в документации помечен «Available for Business Discovery API only» —
# ради него всё и затевается: это просмотры чужого рилса, которых нет в обычном IG Media.
# shortcode Business Discovery НЕ отдаёт (проверено щупом 11.09.2026) — достаём из permalink.
MEDIA_FIELDS = (
    "id,caption,like_count,comments_count,view_count,"
    "media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp"
)
PROFILE_FIELDS = (
    "followers_count,media_count,username,name,biography,website,profile_picture_url,follows_count"
)


def shortcode_from(permalink: str | None) -> str:
    """ReelScribe дедуплицирует по shortcode, а Business Discovery его не отдаёт —
    вынимаем из permalink вида https://www.instagram.com/reel/<shortcode>/."""
    if not permalink:
        return "?"
    parts = [p for p in permalink.rstrip("/").split("/") if p]
    return parts[-1] if parts else "?"


def read_env(path: Path) -> dict[str, str]:
    """Минимальный парсер .env — без зависимостей и без печати значений."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def build_fields(username: str, limit: int) -> str:
    return (
        f"business_discovery.username({username})"
        f"{{{PROFILE_FIELDS},media.limit({limit}){{{MEDIA_FIELDS}}}}}"
    )


def ask(ig_user_id: str, token: str, fields: str, api_version: str) -> tuple[int, dict]:
    base = "https://graph.facebook.com"
    url = f"{base}/{api_version}/{ig_user_id}" if api_version else f"{base}/{ig_user_id}"
    with httpx.Client(timeout=60) as client:
        r = client.get(url, params={"fields": fields, "access_token": token})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"error": {"message": r.text[:500]}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Проба Business Discovery по одному нику")
    parser.add_argument("username", help="ник Instagram без @")
    parser.add_argument("--limit", type=int, default=25, help="сколько последних медиа запросить")
    parser.add_argument("--api-version", default="v23.0", help="версия Graph API (пусто — дефолтная)")
    args = parser.parse_args()

    env = read_env(ENV_FILE)
    token = env.get("META_IG_TOKEN")
    ig_user_id = env.get("META_IG_USER_ID")
    if not token or not ig_user_id:
        print(f"✗ В {ENV_FILE} нет META_IG_TOKEN и/или META_IG_USER_ID.")
        print("  Добавь обе строки и запусти снова. Значения в вывод не печатаются.")
        return 2

    fields = build_fields(args.username, args.limit)
    status, payload = ask(ig_user_id, token, fields, args.api_version)

    # Если версия API не существует — пробуем дефолтную версию приложения.
    if status != 200 and "Unsupported" in json.dumps(payload, ensure_ascii=False):
        print(f"… версия {args.api_version} не подошла, пробую дефолтную")
        status, payload = ask(ig_user_id, token, fields, "")

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = OUT_DIR / f"bd-{args.username}-{stamp}.json"
    out_file.write_text(
        json.dumps({"status": status, "response": payload}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    if status != 200 or "error" in payload:
        err = payload.get("error", {})
        print(f"✗ HTTP {status}")
        print(f"  сообщение: {err.get('message')}")
        print(f"  тип: {err.get('type')}  код: {err.get('code')}  подкод: {err.get('error_subcode')}")
        print(f"\nСырой ответ: {out_file}")
        return 1

    bd = (payload.get("business_discovery") or {})
    media = (bd.get("media") or {}).get("data") or []

    print(f"✓ Аккаунт @{bd.get('username')} — {bd.get('name')}")
    print(f"  подписчиков: {bd.get('followers_count')}   всего публикаций: {bd.get('media_count')}")
    print(f"  медиа в ответе: {len(media)}")

    if not media:
        print("\n⚠ Медиа не пришли — проверь, что аккаунт публичный Business/Creator.")
        print(f"\nСырой ответ: {out_file}")
        return 0

    # Главное, ради чего проба: какие поля реально пришли, а какие Meta молча опустила.
    print("\nЗаполненность полей по пришедшим медиа:")
    for field in ("permalink", "view_count", "like_count", "comments_count",
                  "media_url", "caption", "media_product_type", "timestamp"):
        filled = sum(1 for m in media if m.get(field) not in (None, ""))
        mark = "✓" if filled else "✗"
        print(f"  {mark} {field:20} {filled}/{len(media)}")

    reels = [m for m in media if m.get("media_product_type") == "REELS"]
    print(f"\nИз них рилсов: {len(reels)}")

    followers = bd.get("followers_count") or 0
    print("\nТоп-5 по просмотрам (залётность = просмотры ÷ подписчики):")
    ranked = sorted(media, key=lambda m: m.get("view_count") or 0, reverse=True)[:5]
    for m in ranked:
        views = m.get("view_count")
        er = f"{views / followers:.1f}×" if views and followers else "—"
        caption = (m.get("caption") or "").replace("\n", " ")[:50]
        code = shortcode_from(m.get("permalink"))
        print(f"  {code:14} просм {str(views or '—'):>9}  зал {er:>6}  {caption}")

    print(f"\nСырой ответ: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
