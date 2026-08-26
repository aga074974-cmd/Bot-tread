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


async def add_completed(store, *orders_and_status: tuple[Order, OrderStatus]) -> None:
    """Insert an order and immediately move it past pending — this is what
    the dashboard's three-item cap actually applies to."""
    for order, status in orders_and_status:
        await store.insert(order)
        order.status = status
        await store.update_status(order, error=None)


# --------------------------------------------------------------------------
# the pending-orders bug: they must never be pushed into history
# --------------------------------------------------------------------------

async def test_pending_orders_all_stay_on_the_dashboard_however_many_there_are(
    panel_client: TestClient, panel_store
):
    """The bug this guards against: a pending order older than the newest
    three (of anything) used to fall into /history, where it could no longer
    be cancelled."""
    await add(panel_store, *(an_order(f"نماد{i}", hours=i) for i in range(1, 6)))  # 5 pending

    body = panel_client.get("/").text

    for i in range(1, 6):
        assert f"نماد{i}" in body
    assert 'href="/history"' not in body  # nothing completed to hide


async def test_an_old_pending_order_is_not_hidden_behind_newer_completed_ones(
    panel_client: TestClient, panel_store
):
    """A pending order older than three completed ones is exactly the case
    that was broken: sorted by scheduled_at alone, it would rank 4th and be
    cut off."""
    old_pending = an_order("قدیمی", hours=-100)
    await add(panel_store, old_pending)
    await add_completed(
        panel_store,
        (an_order("الف", hours=1), OrderStatus.SENT),
        (an_order("ب", hours=2), OrderStatus.SENT),
        (an_order("ج", hours=3), OrderStatus.SENT),
    )

    body = panel_client.get("/").text

    assert "قدیمی" in body  # still on the dashboard, not bumped to history
    assert 'href="/history"' not in body  # completed count is exactly 3, nothing hidden


async def test_a_cancel_button_survives_for_every_pending_order_shown(
    panel_client: TestClient, panel_store
):
    await add(panel_store, *(an_order(f"نماد{i}", hours=i) for i in range(1, 6)))

    body = panel_client.get("/")
    assert body.text.count('action="/orders/') == 5


# --------------------------------------------------------------------------
# what the dashboard carries (completed orders only, past the cap of three)
# --------------------------------------------------------------------------

async def test_the_dashboard_stops_at_three_completed_orders(panel_client: TestClient, panel_store):
    """Five completed orders would push the form off a phone screen; the rest
    live in the history page."""
    await add_completed(
        panel_store,
        *((an_order(f"نماد{i}", hours=i), OrderStatus.SENT) for i in range(1, 6)),
    )

    body = panel_client.get("/").text

    assert "نماد5" in body and "نماد4" in body and "نماد3" in body  # newest three
    assert "نماد2" not in body and "نماد1" not in body
    assert 'href="/history"' in body
    assert "2 سفارش دیگر" in body


async def test_no_history_link_when_everything_fits(panel_client: TestClient, panel_store):
    await add_completed(
        panel_store,
        *((an_order(f"نماد{i}", hours=i), OrderStatus.SENT) for i in range(1, 4)),
    )

    body = panel_client.get("/").text

    assert 'href="/history"' not in body


async def test_pending_and_completed_share_the_dashboard_correctly(
    panel_client: TestClient, panel_store
):
    """The realistic mix: some still waiting, more already finished than fit."""
    await add(panel_store, an_order("درانتظار۱", hours=10), an_order("درانتظار۲", hours=11))
    await add_completed(
        panel_store,
        *((an_order(f"تمام{i}", hours=i), OrderStatus.SENT) for i in range(1, 6)),
    )

    body = panel_client.get("/").text

    assert "درانتظار۱" in body and "درانتظار۲" in body  # every pending order
    assert "تمام5" in body and "تمام4" in body and "تمام3" in body  # newest three completed
    assert "تمام2" not in body and "تمام1" not in body
    assert "2 سفارش دیگر" in body


async def test_history_holds_every_order(panel_client: TestClient, panel_store):
    await add_completed(
        panel_store,
        *((an_order(f"نماد{i}", hours=i), OrderStatus.SENT) for i in range(1, 6)),
    )

    body = panel_client.get("/history").text

    for i in range(1, 6):
        assert f"نماد{i}" in body


async def test_history_needs_a_login(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get("/history", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"


# --------------------------------------------------------------------------
# the title
# --------------------------------------------------------------------------

async def test_the_title_is_two_lines_the_brand_in_yellow(panel_client: TestClient):
    body = panel_client.get("/").text

    assert '<span class="brand">(کارگزاری مفید)</span>' in body
    assert "h1 .brand { display: block;" in body  # forces the brand onto its own line


async def test_the_session_light_sits_between_the_two_words_of_the_title(panel_client: TestClient):
    """پنل [چراغ] ربات — the light interrupts the title text itself, in the
    same line, rather than sitting beside the whole title as a block."""
    body = panel_client.get("/").text

    h1_start = body.index("<h1>")
    h1_html = body[h1_start:body.index("</h1>", h1_start)]

    panel_word_pos = h1_html.index("پنل")
    light_pos = h1_html.index('id="session-light"')
    robot_word_pos = h1_html.index("ربات")

    assert panel_word_pos < light_pos < robot_word_pos


async def test_the_robot_badge_sits_inline_with_the_header_title(panel_client: TestClient):
    """Decorative only: inline SVG/CSS, no image or script dependency, and
    hidden from screen readers so it is not read aloud as stray shapes. It
    lives inside .top, alongside the <h1>, not as a block below it."""
    body = panel_client.get("/").text

    assert 'class="robot-badge" aria-hidden="true"' in body
    assert "<svg viewBox=" in body
    assert "@keyframes wave" in body
    assert "wave-arm" in body

    top_start = body.index('class="top"')
    h1_pos = body.index("<h1>", top_start)
    badge_pos = body.index('class="robot-badge"', top_start)
    manual_login_pos = body.index('id="manual-login"', top_start)

    # same header row: badge comes after <h1>, both inside .top, and the
    # whole header is done before the manual-login section and order form.
    assert top_start < h1_pos < badge_pos < manual_login_pos < body.index("سفارش جدید")


# --------------------------------------------------------------------------
# the login-session status light
# --------------------------------------------------------------------------

async def test_the_light_reflects_a_session_recorded_as_valid(panel_client: TestClient, panel_session_store):
    await panel_session_store.record_login_success()

    body = panel_client.get("/").text

    assert 'id="session-light"' in body
    assert 'class="session-light session-light-valid"' in body
    # the class name also appears in the page's own CSS, so check the
    # rendered attribute specifically rather than the bare substring.
    assert 'class="session-light session-light-invalid"' not in body


async def test_the_light_defaults_to_invalid_before_any_check_ever_ran(panel_client: TestClient):
    body = panel_client.get("/").text

    assert 'session-light session-light-invalid' in body


async def test_the_light_turns_invalid_again_after_a_login_failure(
    panel_client: TestClient, panel_session_store
):
    await panel_session_store.record_login_success()
    await panel_session_store.record_login_failure()

    body = panel_client.get("/").text

    assert 'session-light session-light-invalid' in body


async def test_clicking_the_light_is_wired_to_the_check_endpoint(panel_client: TestClient):
    body = panel_client.get("/").text

    assert 'onclick="checkSession()"' in body
    assert "fetch('/session/check'" in body


# --------------------------------------------------------------------------
# the manual-login fallback
# --------------------------------------------------------------------------

HIDDEN_MANUAL_LOGIN = '<div id="manual-login" class="card" hidden>'
SHOWN_MANUAL_LOGIN = '<div id="manual-login" class="card">'


async def test_manual_login_form_stays_hidden_below_the_failure_threshold(
    panel_client: TestClient, panel_main, panel_session_store
):
    for _ in range(panel_main.MANUAL_LOGIN_THRESHOLD - 1):
        await panel_session_store.record_login_failure()

    body = panel_client.get("/").text

    assert HIDDEN_MANUAL_LOGIN in body
    assert SHOWN_MANUAL_LOGIN not in body


async def test_manual_login_form_appears_once_the_threshold_is_reached(
    panel_client: TestClient, panel_main, panel_session_store
):
    for _ in range(panel_main.MANUAL_LOGIN_THRESHOLD):
        await panel_session_store.record_login_failure()

    body = panel_client.get("/").text

    assert SHOWN_MANUAL_LOGIN in body
    assert HIDDEN_MANUAL_LOGIN not in body
    assert 'name="username"' in body
    assert 'type="password" name="password"' in body
    assert 'name="save" value="1"' in body
    assert 'action="/session/manual-login"' in body


async def test_the_robot_badge_is_not_clipped(panel_client: TestClient):
    """The badge's own box and its inner svg must both allow overflow, or
    parts of the SVG art (which extends past its nominal box at the top of
    the waving arm's swing) would get cut off."""
    body = panel_client.get("/").text

    assert ".robot-badge { flex-shrink: 0; width: 120px; height: 88px; overflow: visible; }" in body
    assert "overflow: visible; display: block;" in body


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


async def test_the_header_has_no_screenshots_link(panel_client: TestClient, panel_store):
    """The separate /screenshots page is gone — each order's own gallery
    replaced it."""
    body = panel_client.get("/").text

    assert 'href="/screenshots"' not in body
    assert "عکس‌ها</a>" not in body


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
