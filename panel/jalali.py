from __future__ import annotations

from datetime import date, datetime

import jdatetime

PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def jalali_to_gregorian_str(year: int, month: int, day: int) -> str:
    """Raises ValueError for an invalid Jalali date (e.g. day 31 in a 30-day month)."""
    g = jdatetime.date(year, month, day).togregorian()
    return g.strftime("%Y-%m-%d")


def to_jalali_str(dt: datetime | date) -> str:
    j = jdatetime.date.fromgregorian(date=dt.date() if isinstance(dt, datetime) else dt)
    return f"{j.year}-{j.month:02d}-{j.day:02d} ({PERSIAN_MONTHS[j.month - 1]})"


def current_jalali_year() -> int:
    return jdatetime.date.today().year
