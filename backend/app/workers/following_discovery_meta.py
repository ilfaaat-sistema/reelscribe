"""Дозаполняет карточки готового снимка через Business Discovery."""
from __future__ import annotations

import argparse
from collections import Counter

from app.radar.following_discovery import DiscoveryError, normalize_input, resume_meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="Ник или ссылка на профиль донора")
    parser.add_argument("--batches", type=int, default=20, help="Максимум порций за запуск, до 8 запросов Meta в порции")
    args = parser.parse_args()
    if not 1 <= args.batches <= 50:
        parser.error("Число порций должно быть от 1 до 50")
    try:
        snapshot, retry_at = resume_meta(normalize_input(args.profile), max_batches=args.batches)
    except DiscoveryError as exc:
        print(exc.message)
        return 1
    counts = Counter(row.profile_status for row in snapshot.rows)
    print(f"@{snapshot.username} · {snapshot.collected} подписок · с карточками {counts['available']} · "
          f"закрыто или недоступно {counts['private'] + counts['unavailable']} · "
          f"не проверено {counts[None] + counts['pending'] + counts['not_checked']}")
    if snapshot.reason:
        print(snapshot.reason)
    if retry_at:
        print(f"Продолжение после {retry_at.astimezone():%Y-%m-%d %H:%M:%S %Z}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
