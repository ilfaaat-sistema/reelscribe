"""Московские календарные границы и фильтры сохранённых каруселей."""
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')


def rolling_window(now: datetime, days: int):
    if days not in (14, 30) or now.tzinfo is None:
        raise ValueError('Нужно окно 14/30 дней и время с часовым поясом')
    today = now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1), today + timedelta(days=1)


def history_window(now: datetime):
    if now.tzinfo is None:
        raise ValueError('Нужно время с часовым поясом')
    today = now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.year * 12 + today.month - 1 - 4
    year, zero_month = divmod(month, 12)
    start = today.replace(year=year, month=zero_month + 1,
                          day=min(today.day, calendar.monthrange(year, zero_month + 1)[1]))
    return start, today + timedelta(days=1)


def date_window(first, last):
    first = date.fromisoformat(first) if isinstance(first, str) else first
    last = date.fromisoformat(last) if isinstance(last, str) else last
    if not isinstance(first, date) or not isinstance(last, date) or first > last or last == date.max:
        raise ValueError('Укажите корректные даты с и по')
    return (datetime.combine(first, datetime.min.time(), MOSCOW),
            datetime.combine(last + timedelta(days=1), datetime.min.time(), MOSCOW))


def matches_metrics(item, *, min_likes=None, max_likes=None, min_comments=None, max_comments=None):
    for field, lower, upper in (('likes', min_likes, max_likes), ('comments', min_comments, max_comments)):
        if lower is None and upper is None:
            continue
        value = item.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return False
        if (lower is not None and value < lower) or (upper is not None and value > upper):
            return False
    return True
