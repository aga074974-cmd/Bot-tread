from __future__ import annotations

import re

"""Turns the connector's errors into something a person reads on a phone.

The raw text stays in the database and in the log for debugging; what the panel
shows is Persian, because that is the language of whoever is looking at it.
"""

# When the broker itself says what went wrong ("your password is wrong"), that
# beats any wording of ours — and it is already Persian.
_SITE_SAYS = re.compile(r"the site says:\s*(.+?)(?:\.\s+Still at:|$)", re.S)

# Matched in order, so put the specific messages above the general ones.
_MESSAGES: list[tuple[str, str]] = [
    (
        "page never showed either the login form or the app",
        "صفحه‌ی کارگزاری بالا نیامد — اینترنت سرور یا خود سایت مفید را بررسی کنید.",
    ),
    (
        "login did not reach the app",
        "ورود انجام نشد — نام کاربری/رمز اشتباه است یا یک مرحله‌ی اضافه مثل رمز یکبارمصرف در راه است.",
    ),
    (
        "could not find the login button",
        "دکمه‌ی ورود در صفحه پیدا نشد — احتمالاً سایت مفید عوض شده.",
    ),
    (
        "could not enter the username",
        "نام کاربری در فرم ورود وارد نشد — احتمالاً سایت مفید عوض شده.",
    ),
    (
        "could not enter the password",
        "رمز عبور در فرم ورود وارد نشد — احتمالاً سایت مفید عوض شده.",
    ),
    (
        "the quantity box would not clear",
        "کادر تعداد خالی نشد؛ سفارش فرستاده نشد تا تعدادِ اشتباه ثبت نشود.",
    ),
    (
        "the quantity box reads",
        "تعداد داخل کادر با تعداد سفارش یکی نشد؛ سفارش فرستاده نشد.",
    ),
    (
        "order was sent, but no message matching",
        "سفارش فرستاده شد ولی پیام تأیید شناخته نشد — ممکن است ثبت شده باشد؛ حتماً پرتفوی را چک کنید.",
    ),
    (
        "unexpected error placing order",
        "خطای پیش‌بینی‌نشده هنگام ثبت سفارش — عکس‌های اشکال‌زدایی را ببینید.",
    ),
    (
        "Timeout",
        "زمان انتظار برای صفحه‌ی کارگزاری تمام شد — عکس‌های اشکال‌زدایی را ببینید.",
    ),
]

FALLBACK = "خطای ناشناخته — جزئیات در لاگ سرور و صفحه‌ی عکس‌های اشکال‌زدایی است."


def to_persian(raw: str | None) -> str:
    """The Persian sentence to show for one order's error, or "" if it has none."""
    if not raw or not raw.strip():
        return ""

    said = _SITE_SAYS.search(raw)
    if said and said.group(1).strip():
        return said.group(1).strip()

    for fragment, message in _MESSAGES:
        if fragment in raw:
            return message

    return FALLBACK
