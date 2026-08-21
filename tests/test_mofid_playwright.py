"""End-to-end tests for the Playwright broker connector.

Every test drives the real MofidPlaywrightClient in a real (headless) mobile
browser against tests/fake_site, a stand-in for m.easytrader.ir that uses the
same Persian labels and screen order as the live app.
"""
from __future__ import annotations

import json
import logging
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.broker import mofid_playwright
from bot.broker.base import BrokerError
from bot.broker.mofid_playwright import (
    MofidPlaywrightClient,
    _submit_button_text,
    _submit_selectors,
)
from bot.models import Order, OrderType, Side
from conftest import (
    PASSWORD,
    SHIPPED_SETTLE_MS,
    USERNAME,
    has_shot,
    page_dump,
    record,
    shots,
)

SYMBOL = "دارونو"


def an_order(**overrides: object) -> Order:
    fields: dict = {
        "symbol": SYMBOL,
        "side": Side.BUY,
        "quantity": 10,
        "scheduled_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return Order(**fields)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------

async def test_credentials_are_typed_as_real_keystrokes(make_client):
    """The login fields are driven by an on-screen-keyboard widget that undoes
    any programmatic value set, so the connector must type them key by key."""
    client = make_client(input="stubborn")

    await client.login()

    seen = await record(client)
    assert seen["loginSubmittedWith"] == [USERNAME, PASSWORD]
    assert seen["typedKeys"]["username"] == len(USERNAME)
    assert seen["typedKeys"]["password"] == len(PASSWORD)


async def test_typing_falls_back_to_fill(make_client, caplog):
    """A field that ignores key events still gets filled the old way."""
    client = make_client(input="nokeys")

    with caplog.at_level(logging.WARNING, logger="bot.broker.mofid_playwright"):
        await client.login()

    assert (await record(client))["loginSubmittedWith"] == [USERNAME, PASSWORD]
    assert "did not take typed input" in caplog.text


async def test_field_that_refuses_both_is_reported(make_client, tmp_path):
    client = make_client(input="reject")

    with pytest.raises(BrokerError, match="could not enter the username"):
        await client.login()

    assert shots(tmp_path / "shots") == ["01_login_page.png", "02_login_input_rejected.png"]
    assert 'id="password"' in page_dump(tmp_path / "shots")


async def test_missing_login_button_is_reported(make_client, tmp_path):
    """The scenario still shows the word ورود, just not as a submit button —
    so this also pins that the button is found by selector, not by its text."""
    client = make_client(button="none")

    with pytest.raises(BrokerError, match="could not find the login button"):
        await client.login()

    assert "03_login_button_missing.png" in shots(tmp_path / "shots")
    assert 'id="user-name"' in page_dump(tmp_path / "shots")


async def test_slow_spa_is_not_mistaken_for_a_live_session(make_client):
    """Regression: reading the page straight after goto() saw an empty SPA and
    wrongly concluded the saved session was still good, so no login happened."""
    client = make_client(boot=1500)

    await client.login()

    seen = await record(client)
    assert seen["restoredSession"] is False
    assert seen["loginSubmittedWith"] == [USERNAME, PASSWORD]


async def test_page_that_never_settles_is_reported(make_client, monkeypatch, tmp_path):
    monkeypatch.setattr(mofid_playwright, "PAGE_READY_TIMEOUT_MS", 1_500)
    client = make_client(stuck=1)

    with pytest.raises(BrokerError, match="never showed either the login form or the app"):
        await client.login()

    assert shots(tmp_path / "shots") == ["01_login_stuck.png"]
    # The whole point of the dump: the screenshot shows a spinner, the markup
    # shows the page really was still empty rather than mis-matched.
    assert "در حال بارگذاری" in page_dump(tmp_path / "shots")


async def test_the_sites_own_error_message_is_reported(make_client, monkeypatch, tmp_path, caplog):
    """When the broker says why it refused, that beats anything we can infer
    from a timeout — so it goes in the error the panel shows and in the log."""
    monkeypatch.setattr(mofid_playwright, "PAGE_READY_TIMEOUT_MS", 1_500)
    client = make_client(badlogin=1)

    with caplog.at_level(logging.ERROR, logger="bot.broker.mofid_playwright"):
        with pytest.raises(BrokerError, match="نام کاربری یا کلمه عبور اشتباه است") as excinfo:
            await client.login()

    assert "the site says" in str(excinfo.value)
    assert "نام کاربری یا کلمه عبور اشتباه است" in caplog.text
    assert "03_login_failed.png" in shots(tmp_path / "shots")


async def test_a_step_with_no_message_falls_back_to_the_generic_reason(
    make_client, monkeypatch, tmp_path
):
    """An OTP/agreement screen carries no #alert-item at all."""
    monkeypatch.setattr(mofid_playwright, "PAGE_READY_TIMEOUT_MS", 1_500)
    client = make_client(badlogin=1, alert="none")

    with pytest.raises(BrokerError, match="OTP/agreement") as excinfo:
        await client.login()

    assert "the site says" not in str(excinfo.value)
    assert "127.0.0.1" in str(excinfo.value)  # reports where it got stuck
    assert "رمز یکبار مصرف" in page_dump(tmp_path / "shots")  # the step in the way


async def test_an_empty_error_banner_is_ignored(make_client, monkeypatch):
    """The banner element is always in the DOM; only its text is worth
    reporting, and a hidden one has none."""
    monkeypatch.setattr(mofid_playwright, "PAGE_READY_TIMEOUT_MS", 1_500)
    client = make_client(badlogin=1, alert="empty")

    with pytest.raises(BrokerError) as excinfo:
        await client.login()

    assert "the site says" not in str(excinfo.value)
    assert "wrong credentials" in str(excinfo.value)


async def test_session_is_saved_and_reused(make_client, tmp_path, caplog):
    state = tmp_path / "auth_state.json"

    first = make_client(storage_state_path=state, screenshot_dir=tmp_path / "run1")
    await first.login()
    assert (await record(first))["loginSubmittedWith"] == [USERNAME, PASSWORD]
    await first.close()

    assert json.loads(state.read_text(encoding="utf-8"))["origins"]
    assert stat.S_IMODE(state.stat().st_mode) == 0o600  # holds session cookies

    second = make_client(storage_state_path=state, screenshot_dir=tmp_path / "run2")
    with caplog.at_level(logging.INFO, logger="bot.broker.mofid_playwright"):
        await second.login()

    seen = await record(second)
    assert seen["restoredSession"] is True
    assert seen["loginSubmittedWith"] is None  # credentials never re-entered
    assert "restored saved session" in caplog.text


async def test_unreadable_session_file_is_ignored(make_client, tmp_path, caplog):
    state = tmp_path / "auth_state.json"
    state.write_text("not json at all", encoding="utf-8")
    client = make_client(storage_state_path=state)

    with caplog.at_level(logging.WARNING, logger="bot.broker.mofid_playwright"):
        await client.login()

    assert "is unreadable, ignoring" in caplog.text
    assert (await record(client))["loginSubmittedWith"] == [USERNAME, PASSWORD]


@pytest.mark.parametrize("only", ["watch", "search", "portfolio", "order"])
async def test_any_single_app_marker_counts_as_reaching_the_app(make_client, only: str):
    """The bottom bar's own data-cy hooks are combined with or_ precisely so a
    missing or renamed one does not read as a failed login."""
    client = make_client(markers=only)

    await client.login()

    assert (await record(client))["loginSubmittedWith"] == [USERNAME, PASSWORD]


async def test_browser_pretends_to_be_a_phone(make_client):
    """The selectors in this module were captured from the mobile layout; a
    desktop viewport/user-agent would be served a different site."""
    client = make_client()

    await client.login()

    seen = await record(client)
    assert "Android" in seen["userAgent"]
    assert seen["hasTouch"] is True
    assert seen["viewportWidth"] < 500


# --------------------------------------------------------------------------
# place_order
# --------------------------------------------------------------------------

async def test_dry_run_fills_the_ticket_but_never_submits(make_client):
    client = make_client(dry_run=True)
    order = an_order(quantity=7)
    await client.login()

    ticket_id = await client.place_order(order)

    assert ticket_id == f"dry-run-{order.id}"
    seen = await record(client)
    assert seen["symbolOpened"] == SYMBOL
    assert seen["submitClicked"] is False
    box = client._page.locator(mofid_playwright.QUANTITY_INPUT)
    assert mofid_playwright._digits(await box.input_value()) == "7"


async def test_live_buy_order_is_sent(make_client):
    client = make_client(dry_run=False)
    order = an_order(quantity=10)
    await client.login()

    ticket_id = await client.place_order(order)

    assert ticket_id == f"submitted-{order.id}"
    seen = await record(client)
    assert seen["searchedFor"] == SYMBOL
    assert seen["side"] == "buy"
    assert seen["quantity"] == "10"
    assert seen["submitClicked"] is True


async def test_the_ticket_is_confirmed_to_be_for_the_right_symbol(make_client, tmp_path):
    """The search matches on part of a name, so it can open a neighbour's
    ticket. A correctly sized order on the wrong symbol is the worst outcome
    available here, so it is checked before a single digit is entered."""
    client = make_client(dry_run=False, symbol="wrong")
    await client.login()

    with pytest.raises(BrokerError, match="landed on a different symbol"):
        await client.place_order(an_order())

    seen = await record(client)
    assert seen["keypadPresses"] == []  # stopped before touching the numbers
    assert seen["submitClicked"] is False
    assert has_shot(tmp_path / "shots", "wrong_symbol")


async def test_a_ticket_that_will_not_say_its_symbol_stops_the_order(make_client, tmp_path):
    """Fail closed: an order that cannot be confirmed is not worth sending."""
    client = make_client(dry_run=False, symbol="missing")
    await client.login()

    with pytest.raises(BrokerError, match="not ordering on a ticket we cannot read"):
        await client.place_order(an_order())

    assert (await record(client))["submitClicked"] is False
    assert has_shot(tmp_path / "shots", "symbol_unverified")


async def test_the_same_symbol_spelled_differently_still_passes(make_client):
    """Arabic yeh and kaf, and stray spaces, are the same symbol — the guard
    must not refuse a perfectly good order over how the text is written.
    کویر is the test's symbol here precisely because it has both letters."""
    client = make_client(dry_run=False, symbol="arabic")
    order = an_order(symbol="کویر")
    await client.login()

    assert await client.place_order(order) == f"submitted-{order.id}"


async def test_the_quantity_box_is_emptied_before_typing(make_client):
    """The box opens pre-filled from the buying power, and being readonly it
    can only be emptied by backspacing on the app's keypad. Whatever it held
    must be gone: the order goes out for the number we asked for."""
    client = make_client(dry_run=False)
    await client.login()

    await client.place_order(an_order(quantity=80))

    seen = await record(client)
    assert seen["quantity"] == "80"
    assert seen["keypadPresses"][-2:] == ["8", "0"]  # entered key by key
    assert seen["keypadPresses"][:4] == ["back"] * 4  # after clearing the prefill


@pytest.mark.parametrize("keypad", ["text", "persian", "cy"])
async def test_the_keypad_is_driven_whatever_its_keys_look_like(make_client, keypad: str):
    """The keypad's own markup has not been captured, so the connector tries
    several shapes: data-cy hooks, ASCII digits, Persian digits."""
    client = make_client(dry_run=False, keypad=keypad)
    order = an_order(quantity=1200)
    await client.login()

    assert await client.place_order(order) == f"submitted-{order.id}"
    assert (await record(client))["quantity"] == "1200"


async def test_a_box_that_never_opens_a_keypad_stops_the_order(make_client, tmp_path):
    """Readonly box, no keypad: there is no way in, and pretending otherwise
    would submit whatever the box happened to hold."""
    client = make_client(dry_run=False, keypad="none")
    await client.login()

    with pytest.raises(BrokerError, match="did not bring up the app's keypad"):
        await client.place_order(an_order(quantity=80))

    assert (await record(client))["submitClicked"] is False
    assert has_shot(tmp_path / "shots", "quantity_keypad_missing")


async def test_an_unrecognised_keypad_is_recorded_not_guessed_at(make_client, tmp_path):
    """A keypad we cannot identify is a stop, not a reason to start clicking
    around an order form. The markup is kept so the keys can be named."""
    client = make_client(dry_run=False, keypad="alien")
    await client.login()

    with pytest.raises(BrokerError, match="none of its keys were recognised"):
        await client.place_order(an_order(quantity=80))

    seen = await record(client)
    assert seen["keypadPresses"] == []  # nothing was pressed on a guess
    assert seen["submitClicked"] is False
    assert (tmp_path / "shots" / mofid_playwright.KEYPAD_HTML_NAME).exists()


async def test_the_open_keypad_is_kept_on_record(make_client, tmp_path):
    """Every run saves the keypad's markup, working or not — that is how its
    keys get named without another live order."""
    client = make_client(dry_run=True)
    await client.login()

    await client.place_order(an_order(quantity=80))

    keypad_html = (tmp_path / "shots" / mofid_playwright.KEYPAD_HTML_NAME).read_text(encoding="utf-8")
    assert "keyboard-open" in keypad_html
    assert has_shot(tmp_path / "shots", "quantity_keypad")


async def test_a_box_that_will_not_empty_stops_the_order(make_client, tmp_path):
    client = make_client(dry_run=False, qty="sticky")
    await client.login()

    with pytest.raises(BrokerError, match="would not clear"):
        await client.place_order(an_order(quantity=80))

    assert (await record(client))["submitClicked"] is False
    assert has_shot(tmp_path / "shots", "quantity_not_cleared")


async def test_a_box_holding_the_wrong_number_stops_the_order(make_client, tmp_path):
    """Pressed 8 and 0, box reads 99: sending that would buy the wrong amount."""
    client = make_client(dry_run=False, qty="garbled")
    await client.login()

    with pytest.raises(BrokerError, match="not sending an order for the wrong amount"):
        await client.place_order(an_order(quantity=80))

    assert (await record(client))["submitClicked"] is False
    assert has_shot(tmp_path / "shots", "quantity_wrong")


async def test_the_entered_quantity_is_photographed(make_client, tmp_path):
    """A screenshot of the box with the amount in it, before anything is sent."""
    client = make_client(dry_run=True)
    await client.login()

    await client.place_order(an_order(quantity=80))

    assert has_shot(tmp_path / "shots", "quantity_filled")


async def test_market_order_keeps_the_prefilled_price(make_client):
    client = make_client(dry_run=False)
    await client.login()

    await client.place_order(an_order(order_type=OrderType.MARKET))

    assert (await record(client))["price"] == "12500"  # the ticket's own default


async def test_limit_order_overrides_the_price(make_client):
    """The price box is readonly too, so it takes the same keypad route."""
    client = make_client(dry_run=False)
    await client.login()

    await client.place_order(an_order(order_type=OrderType.LIMIT, price=9800))

    assert (await record(client))["price"] == "9800"


async def test_sell_order_uses_the_sell_controls(make_client):
    client = make_client(dry_run=False)
    await client.login()

    await client.place_order(an_order(side=Side.SELL))

    seen = await record(client)
    assert seen["side"] == "sell"
    assert seen["submitClicked"] is True


async def test_a_buy_button_without_its_hook_is_still_found(make_client):
    """Only the sell hook was seen on a captured page; the buy one is inferred,
    so the button's Persian text stays behind it."""
    client = make_client(dry_run=False, buycy="none")
    order = an_order()
    await client.login()

    assert await client.place_order(order) == f"submitted-{order.id}"
    assert (await record(client))["submitClicked"] is True


async def test_no_confirmation_step_is_taken(make_client):
    """Sending is final at this broker. The scenario puts a تایید button on
    screen anyway: clicking one would be a second, unwanted action on a live
    order, so nothing may touch it."""
    client = make_client(dry_run=False, confirm_trap=1)
    order = an_order()
    await client.login()

    ticket_id = await client.place_order(order)

    assert ticket_id == f"submitted-{order.id}"
    assert (await record(client))["confirmClicked"] is False


async def test_a_second_screenshot_follows_the_first_after_a_pause(make_client, tmp_path, monkeypatch):
    """The ticket answers a beat after the click, so the shot taken on the
    spot shows the form mid-flight and the later one shows the outcome — they
    must not be the same picture."""
    monkeypatch.setattr(mofid_playwright, "SUBMIT_SETTLE_MS", SHIPPED_SETTLE_MS)
    client = make_client(dry_run=False, reply=600)  # reply lands mid-wait
    await client.login()

    await client.place_order(an_order())

    first = (tmp_path / "shots" / "11_after_submit.png").stat().st_size
    second = (tmp_path / "shots" / "12_after_submit_1s.png").stat().st_size
    assert first != second, "both screenshots caught the same screen"


async def test_missing_success_message_is_reported(make_client, tmp_path):
    """SUCCESS_TEXT is still a guess, so a run that does not recognise the
    reply has to keep the markup — that is where the real wording is."""
    client = make_client(dry_run=False, outcome="nosuccess")
    await client.login()

    with pytest.raises(BrokerError, match="no message matching") as excinfo:
        await client.place_order(an_order())

    assert "may well have gone through" in str(excinfo.value)
    assert mofid_playwright.PAGE_HTML_NAME in str(excinfo.value)
    assert has_shot(tmp_path / "shots", "no_confirmation")
    assert "در حال پردازش" in page_dump(tmp_path / "shots")


async def test_the_unrecognised_reply_is_dumped_where_it_happens(make_client, tmp_path):
    """place_order's catch-all dumps the page for any error, which would hide
    a regression here — so this drives the inner step directly. The markup has
    to be kept at the moment the reply goes unrecognised, whatever the layer
    above happens to do."""
    client = make_client(dry_run=False, outcome="nosuccess")
    await client.login()

    with pytest.raises(BrokerError, match="no message matching"):
        await client._do_place_order(an_order())

    dump = page_dump(tmp_path / "shots")
    assert dump is not None, "the unrecognised reply left no page.html behind"
    assert "در حال پردازش" in dump


async def test_every_step_leaves_a_numbered_screenshot(make_client, tmp_path):
    client = make_client(dry_run=False)
    await client.login()

    await client.place_order(an_order())

    assert shots(tmp_path / "shots") == [
        "01_login_page.png",
        "02_login_filled.png",
        "03_login_done.png",
        "04_landing.png",
        "05_search.png",
        "06_symbol_page.png",
        "07_ticket_opened.png",
        "08_quantity_keypad.png",
        "09_quantity_filled.png",
        "10_form_filled.png",
        "11_after_submit.png",
        "12_after_submit_1s.png",
    ]


async def test_browser_failures_are_reported_as_broker_errors(make_client, tmp_path, caplog):
    """Regression: anything Playwright raises must arrive as a BrokerError so
    the scheduler retries and marks the order failed instead of dying."""
    client = make_client(dry_run=False)
    await client.login()
    await client._page.close()

    with caplog.at_level(logging.WARNING, logger="bot.broker.mofid_playwright"):
        with pytest.raises(BrokerError, match="unexpected error placing order"):
            await client.place_order(an_order())

    # Nothing left to photograph or dump, and neither may mask the real error.
    assert "failed to save page HTML" in caplog.text
    assert page_dump(tmp_path / "shots") is None


async def test_a_clean_run_leaves_no_page_dump(make_client, tmp_path):
    """The dump is a failure artefact: a run that worked should not leave one."""
    client = make_client(dry_run=True)
    await client.login()

    await client.place_order(an_order())

    assert page_dump(tmp_path / "shots") is None


async def test_unexpected_errors_keep_their_cause(tmp_path):
    client = MofidPlaywrightClient("u", "p", screenshot_dir=tmp_path / "shots")

    async def boom(order: Order) -> str:
        raise ValueError("kaboom")

    client._do_place_order = boom

    with pytest.raises(BrokerError, match="kaboom") as excinfo:
        await client.place_order(an_order())
    assert isinstance(excinfo.value.__cause__, ValueError)


async def test_broker_errors_are_not_rewrapped(tmp_path):
    client = MofidPlaywrightClient("u", "p", screenshot_dir=tmp_path / "shots")
    original = BrokerError("rejected by the broker")

    async def boom(order: Order) -> str:
        raise original

    client._do_place_order = boom

    with pytest.raises(BrokerError) as excinfo:
        await client.place_order(an_order())
    assert excinfo.value is original


async def test_close_shuts_the_browser_down(make_client):
    client = make_client()
    await client.login()

    await client.close()

    assert client._browser is not None and not client._browser.is_connected()


# --------------------------------------------------------------------------
# selectors
# --------------------------------------------------------------------------

def test_submit_button_text_matches_the_side():
    assert _submit_button_text(Side.BUY) == "ارسال خرید"
    assert _submit_button_text(Side.SELL) == "ارسال فروش"


def test_fake_site_uses_the_same_selectors_as_the_connector():
    """Keeps the stand-in honest: if a selector here is retuned against the
    real site, tests/fake_site/index.html has to be updated with it."""
    html = (Path(__file__).parent / "fake_site" / "index.html").read_text(encoding="utf-8")

    ids = [
        mofid_playwright.USERNAME_INPUT,
        mofid_playwright.PASSWORD_INPUT,
        mofid_playwright.LOGIN_ERROR,
    ]
    missing_ids = [sel for sel in ids if f'id="{sel.lstrip("#")}"' not in html]
    assert not missing_ids, f"tests/fake_site/index.html has no element for: {missing_ids}"
    assert 'type="submit"' in html, f"nothing matches {mofid_playwright.LOGIN_SUBMIT}"

    # data-cy selectors, as [data-cy="name"] — the stand-in must carry each name.
    hooks = [
        *mofid_playwright.APP_MARKERS,
        mofid_playwright.SYMBOL_HEADER,
        mofid_playwright.QUANTITY_INPUT,
        mofid_playwright.PRICE_INPUT,
        # The stand-in writes the side into this one, so only the stem is literal.
        *(_submit_selectors(side)[0].replace(side.value, "") for side in Side),
    ]
    missing_hooks = [h for h in hooks if h.strip("[]").split('"')[1] not in html]
    assert not missing_hooks, f"tests/fake_site/index.html is missing: {missing_hooks}"

    labels = [
        mofid_playwright.SYMBOL_SEARCH_PLACEHOLDER,
        mofid_playwright.BUY_BUTTON_TEXT,
        mofid_playwright.SELL_BUTTON_TEXT,
        mofid_playwright.SUCCESS_TEXT,
        _submit_button_text(Side.BUY),
        _submit_button_text(Side.SELL),
    ]
    missing = [label for label in labels if label not in html]
    assert not missing, f"tests/fake_site/index.html is missing: {missing}"


def test_the_number_boxes_are_readonly_in_the_stand_in():
    """The real ones are. If this ever stops being reproduced, a connector that
    went back to typing into them would pass its tests and fail live."""
    html = (Path(__file__).parent / "fake_site" / "index.html").read_text(encoding="utf-8")
    ticket = html[html.index('data-cy="order-form-input-quantity"') - 200 :]

    assert "readonly" in ticket[:400]
    assert "uikeyboard" in ticket[:400]


def test_fake_login_form_has_no_placeholders():
    """The bug these selectors fixed: those Persian strings are labels above
    the inputs. The stand-in must keep reproducing that, or a connector that
    went back to get_by_placeholder() would pass its tests and fail live."""
    html = (Path(__file__).parent / "fake_site" / "index.html").read_text(encoding="utf-8")
    login_form = html[html.index('<form id="login-form"') : html.index("</form>")]

    assert "placeholder" not in login_form
    assert '<label for="user-name">' in login_form
    assert '<label for="password">' in login_form
