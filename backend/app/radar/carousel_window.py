"""Единые московские календарные границы живых каруселей."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')


def rolling_window(now: datetime, days: int):
    if days not in (14, 30) or now.tzinfo is None:
        raise ValueError('Нужно окно 14/30 дней и время с часовым поясом')
    today = now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1), today + timedelta(days=1)
