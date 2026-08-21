"""The panel's own pages: what the dashboard shows on a phone, and what moved
to the history page."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bot.config import TEHRAN_TZ
from bot.models import Order, OrderStatus, Side

LOGIN_FAILURE = (
    "login did not reach the app — the site says: نام کاربری یا کلمه عبور اشتباه است. "
    "Still at: https://m.easytrader.ir/ — see the login_failed screenshot"
)


# 2026-08-21 19:22 Tehran is 1405/05/30 19:22 — the example on the panel.
BASE_TIME = datetime(2026, 8, 21, 19, 22, tzinfo=TEHRAN_TZ)


def an_order(symbol: str, *, hours: int = 0, quantity: int = 10) -> Order:
    return Order(
        symbol=symbol,
        side=Side.BUY,
        quantity=quantity,
        scheduled_at=BASE_TIME + timedelta(hours=hours),
    )


async def add(store, *orders: Order) -> None:
    for order in orders:
        await store.insert(order)


# --------------------------------------------------------------------------
# what the dashboard carries
# --------------------------------------------------------------------------

async def test_the_dashboard_stops_at_three_orders(panel_client: TestClient, panel_store):
    """Five orders would push the form off a phone screen; the rest live in
    the history page."""
    await add(panel_store, *(an_order(f"نماد{i}", hours=i) for i in range(1, 6)))

    body = panel_client.get("/").text

    assert "نماد5" in body and "نماد4" in body and "نماد3" in body  # newest three
    assert "نماد2" not in body and "نماد1" not in body
    assert 'href="/history"' in body
    assert "2 سفارش دیگر" in body


async def test_no_history_link_when_everything_fits(panel_client: TestClient, panel_store):
    await add(panel_store, *(an_order(f"نماد{i}", hours=i) for i in range(1, 4)))

    body = panel_client.get("/").text

    assert 'href="/history"' not in body


async def test_history_holds_every_order(panel_client: TestClient, panel_store):
    await add(panel_store, *(an_order(f"نماد{i}", hours=i) for i in range(1, 6)))

    body = panel_client.get("/history").text

    for i in range(1, 6):
        assert f"نماد{i}" in body


async def test_history_needs_a_login(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get("/history", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"


# --------------------------------------------------------------------------
# the date, the errors, the header
# --------------------------------------------------------------------------

async def test_the_date_is_digits_only_with_the_time_below(panel_client: TestClient, panel_store):
    """۱۴۰۵/۰۵/۳۰ is 2026-08-21; the month name belongs in the order form's
    dropdown, not in every row."""
    await add(panel_store, an_order("دارونو"))

    body = panel_client.get("/").text

    assert "1405/05/30" in body
    assert '<div class="time">19:22</div>' in body
    assert "(مرداد)" not in body  # the old combined format


async def test_a_failed_order_carries_its_message_on_the_badge(panel_client: TestClient, panel_store):
    """No error text under the status any more: it is on the badge, and a tap
    brings it up."""
    order = an_order("دارونو")
    await add(panel_store, order)
    order.status = OrderStatus.FAILED
    await panel_store.update_status(order, error=LOGIN_FAILURE)

    body = panel_client.get("/").text

    assert 'data-error="نام کاربری یا کلمه عبور اشتباه است"' in body
    assert 'onclick="showError(this)"' in body
    assert "login did not reach the app" not in body  # never the English original
    assert "3000" in body  # and it goes away again


async def test_an_order_that_worked_has_nothing_to_tap(panel_client: TestClient, panel_store):
    order = an_order("دارونو")
    await add(panel_store, order)
    order.status = OrderStatus.SENT
    await panel_store.update_status(order, error=None)

    body = panel_client.get("/").text

    assert "data-error" not in body
    assert '<span class="badge sent">' in body


async def test_the_header_has_no_logout(panel_client: TestClient, panel_store):
    body = panel_client.get("/").text

    assert "/logout" not in body
    assert "خروج" not in body


async def test_logging_out_still_works_by_url(panel_client: TestClient):
    """The link is gone from the phone view, not the ability."""
    response = panel_client.get("/logout", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert panel_client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", ["/", "/history"])
async def test_the_table_can_scroll_sideways(panel_client: TestClient, panel_store, path: str):
    """Six columns do not fit a phone; the table scrolls, the page does not."""
    await add(panel_store, an_order("دارونو"))

    assert '<div class="table-wrap">' in panel_client.get(path).text
