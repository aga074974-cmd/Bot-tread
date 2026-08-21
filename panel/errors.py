from __future__ import annotations

import re

from bot.scheduler import RETRY_DEADLINE_MARK

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
        "the search landed on a different symbol",
        "تیکتی که باز شد مال نماد دیگری بود؛ هیچ سفارشی روی نماد اشتباه فرستاده نشد.",
    ),
    (
        "not ordering on a ticket we cannot read",
        "نام نماد روی تیکت خوانده نشد، پس سفارش فرستاده نشد — احتمالاً اپ مفید عوض شده.",
    ),
    (
        "did not bring up the app's keypad",
        "کیبورد عددی اپ باز نشد و کادر readonly است، پس عدد اصلاً وارد نشد.",
    ),
    (
        "none of its keys were recognised",
        "کیبورد باز شد ولی دکمه‌هایش شناخته نشد؛ هیچ دکمه‌ای کورکورانه زده نشد. "
        "فایل keypad.html را از صفحه‌ی عکس‌ها بفرست.",
    ),
    (
        "box would not clear",
        "کادر خالی نشد؛ سفارش فرستاده نشد تا عددِ اشتباه ثبت نشود.",
    ),
    (
        "box reads",
        "عدد داخل کادر با عدد سفارش یکی نشد؛ سفارش فرستاده نشد.",
    ),
    (
        "on the order form",
        "بخشی از فرم سفارش پیدا نشد — احتمالاً اپ مفید عوض شده.",
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


DEADLINE_FA = "از مهلت مجاز گذشت و تلاش دوباره متوقف شد."


def to_persian(raw: str | None) -> str:
    """The Persian sentence to show for one order's error, or "" if it has none."""
    if not raw or not raw.strip():
        return ""

    # Why it stopped and what went wrong are two different facts, and both
    # matter: one says the order is not coming, the other says what to fix.
    prefix = f"{DEADLINE_FA} " if RETRY_DEADLINE_MARK in raw else ""

    said = _SITE_SAYS.search(raw)
    if said and said.group(1).strip():
        return prefix + said.group(1).strip()

    for fragment, message in _MESSAGES:
        if fragment in raw:
            return prefix + message

    return prefix + FALLBACK
