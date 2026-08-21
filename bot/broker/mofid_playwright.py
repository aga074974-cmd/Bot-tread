from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from bot.broker.base import BrokerClient, BrokerError
from bot.models import Order, OrderType, Side

log = logging.getLogger(__name__)

LOGIN_URL = "https://m.easytrader.ir/"

# Read off the real login page. The Persian strings around these fields are
# labels sitting above the inputs, not placeholders — searching by placeholder
# text matched nothing, which is what these ids replace.
USERNAME_INPUT = "#user-name"
PASSWORD_INPUT = "#password"
LOGIN_SUBMIT = "button[type='submit']"
LOGIN_ERROR = "#alert-item"  # the site's own banner, e.g. a wrong password

# The bottom bar, by the app's own test hooks (see the data-cy note below).
# Any one of them on screen means we are inside the app.
NAVBAR_MARKET_WATCH = '[data-cy="main-navbar-market-watch"]'
NAVBAR_SEARCH = '[data-cy="main-navbar-search"]'
NAVBAR_PORTFOLIO = '[data-cy="main-navbar-portfolio"]'
NAVBAR_ORDER = '[data-cy="main-navbar-order"]'
APP_MARKERS = (NAVBAR_MARKET_WATCH, NAVBAR_SEARCH, NAVBAR_PORTFOLIO, NAVBAR_ORDER)

# The site is an Angular SPA behind an OAuth redirect, so nothing is on the
# page at load time. Every step waits for its element rather than assuming
# it's already there.
PAGE_READY_TIMEOUT_MS = 45_000

# How long to wait for the broker's success message after sending an order.
SUCCESS_TIMEOUT_MS = 15_000

# The ticket does not react to the send click straight away, so the shot taken
# on the spot catches the form mid-flight. This is how long to wait before
# taking a second one that actually shows the outcome.
SUBMIT_SETTLE_MS = 1_000

# Where a failed run dumps the page's markup, next to that run's screenshots.
PAGE_HTML_NAME = "page.html"

# ---------------------------------------------------------------------------
# The app is built with data-cy attributes — its own developers' test hooks.
# They survive rewording, so everything confirmed from a captured order page
# is matched on those. Persian text is left only where no hook was seen: the
# search page and the symbol page have not been captured yet.
#
# (The labels above these boxes are <span class="label-widget">, not
# placeholders — the same trap the login page sprang.)
# ---------------------------------------------------------------------------
SYMBOL_SEARCH_PLACEHOLDER = "جستجوی نماد"
BUY_BUTTON_TEXT = "خرید"
SELL_BUTTON_TEXT = "فروش"

SYMBOL_HEADER = '[data-cy="order-form-header-symbol-name"]'
QUANTITY_INPUT = '[data-cy="order-form-input-quantity"]'
PRICE_INPUT = '[data-cy="order-form-input-price"]'

SUCCESS_TEXT = "با موفقیت"  # unverified — not seen in a captured page yet

# Both number boxes are readonly and carry the app's uikeyboard directive, so
# nothing can be typed into them: clicking one opens the app's own numeric
# keypad. The app announces that by putting keyboard-open on #root.
KEYBOARD_OPEN = "#root.keyboard-open"
KEYPAD_OPEN_TIMEOUT_MS = 5_000
KEYPAD_HTML_NAME = "keypad.html"
MAX_CLEAR_PRESSES = 20
# How many keys a candidate must hold before it counts as the keypad, and
# how many candidates per shape are worth examining.
KEYPAD_MIN_KEYS = 3
MAX_KEYPAD_CANDIDATES = 5

# The keypad's own markup has not been captured yet, so these are the shapes
# worth trying. Every run saves KEYPAD_HTML_NAME while the keypad is open, and
# if none of these match, the run stops: on an order form, clicking something
# we have not identified is worse than not ordering at all.
KEYPAD_CONTAINERS = (
    '[data-cy*="keyboard"]',
    '[data-cy*="keypad"]',
    "ui-keyboard, app-keyboard, keyboard-widget, numeric-keyboard",
    '[class*="keyboard"]:not(#root):not(html):not(body)',
    '[class*="keypad"]',
    '[id*="keyboard"]:not(#root)',
)
KEYPAD_BACKSPACE = (
    '[data-cy*="backspace"]',
    '[data-cy*="delete"]',
    '[data-cy*="clear"]',
    '[class*="backspace"]',
    '[class*="delete"]',
    ':text-is("⌫")',
    ':text-is("حذف")',
)


def _digit_selectors(digit: str) -> tuple[str, ...]:
    """Ways one key of the keypad might be written, best first."""
    persian = "۰۱۲۳۴۵۶۷۸۹"[int(digit)]
    return (
        f'[data-cy="keyboard-key-{digit}"]',
        f'[data-cy$="key-{digit}"]',
        f'[data-value="{digit}"]',
        f'[data-key="{digit}"]',
        f':text-is("{digit}")',
        f':text-is("{persian}")',
    )


# The ticket writes numbers back in Persian digits with a thousands separator
# (۱٬۲۰۰ for a 1200 we typed), so values coming out of it are compared on their
# digits alone.
_ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text.translate(_ASCII_DIGITS))


# Persian text arrives written more than one way: Arabic yeh and kaf for the
# Persian ones, and zero-width joiners that are invisible either way. A symbol
# typed into the panel and the same symbol shown by the app must still compare
# equal, or the guard below would refuse every order.
_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "\u200c": "", "\u200e": "", "\u200f": ""})


def _same_symbol(left: str, right: str) -> bool:
    return " ".join(left.translate(_LETTERS).split()) == " ".join(
        right.translate(_LETTERS).split()
    )


def _submit_button_text(side: Side) -> str:
    return "ارسال خرید" if side == Side.BUY else "ارسال فروش"


def _submit_selectors(side: Side) -> tuple[str, ...]:
    """The sell hook is confirmed from a captured sell ticket; the buy one is
    its obvious counterpart but has not been seen, so the button's own text
    stays behind it as a fallback."""
    return (
        f'[data-cy="oms-order-form-submit-button-{side.value}"]',
        f':text-is("{_submit_button_text(side)}")',
    )


class MofidPlaywrightClient(BrokerClient):
    def __init__(
        self,
        username: str,
        password: str,
        dry_run: bool = True,
        headless: bool = True,
        screenshot_dir: str | Path | None = None,
        storage_state_path: str | None = "auth_state.json",
    ) -> None:
        self.username = username
        self.password = password
        self.dry_run = dry_run
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._shot_dir = Path(screenshot_dir) if screenshot_dir else None
        self._shot_seq = 0
        self._storage_state_path = Path(storage_state_path) if storage_state_path else None

    async def _screenshot(self, label: str) -> None:
        if not self._shot_dir or not self._page:
            return
        self._shot_dir.mkdir(parents=True, exist_ok=True)
        self._shot_seq += 1
        path = self._shot_dir / f"{self._shot_seq:02d}_{label}.png"
        try:
            await self._page.screenshot(path=str(path))
            log.info("saved debug screenshot: %s", path)
        except Exception as exc:
            log.warning("failed to save screenshot %s: %s", path, exc)

    async def _save_page_html(self, name: str = PAGE_HTML_NAME) -> None:
        """Dump the live DOM next to that run's screenshots. The screenshot
        shows where a run stopped; the markup shows why — which is what you
        need to retune a selector that no longer matches."""
        if not self._shot_dir or not self._page:
            return
        self._shot_dir.mkdir(parents=True, exist_ok=True)
        path = self._shot_dir / name
        try:
            html = await self._page.content()
            path.write_text(html, encoding="utf-8")
        except Exception as exc:
            log.warning("failed to save page HTML %s: %s", path, exc)
            return
        log.info("saved page HTML: %s", path)

    async def _capture_failure(self, label: str) -> None:
        """Everything worth keeping about the moment a run gave up."""
        await self._screenshot(label)
        await self._save_page_html()

    def _load_storage_state(self) -> str | None:
        if not self._storage_state_path or not self._storage_state_path.exists():
            return None
        try:
            json.loads(self._storage_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("saved session file %s is unreadable, ignoring: %s", self._storage_state_path, exc)
            return None
        return str(self._storage_state_path)

    async def _save_storage_state(self) -> None:
        if not self._storage_state_path or not self._context:
            return
        await self._context.storage_state(path=str(self._storage_state_path))
        self._storage_state_path.chmod(0o600)  # contains session cookies
        log.info("saved session to %s for next run", self._storage_state_path)

    async def _type_into(self, field, value: str, what: str) -> None:
        """Type a value the way a person would. The login inputs have their own
        on-screen-keyboard widget, and such fields often ignore a programmatic
        value set — real key events are what their JS listens for. Falls back to
        fill() and verifies the value actually landed either way."""
        await field.click()
        await field.press_sequentially(value, delay=30)

        if await field.input_value() == value:
            return

        log.warning("%s field did not take typed input, retrying with fill()", what)
        await field.fill(value)
        if await field.input_value() != value:
            await self._capture_failure("login_input_rejected")
            raise BrokerError(
                f"could not enter the {what} — the field rejected both typing and fill(); "
                "see the login_input_rejected screenshot"
            )

    def _app_markers(self) -> Locator:
        """Anything only the logged-in app puts on screen. Left un-narrowed so
        it can be combined with another locator; add .first before waiting."""
        assert self._page is not None
        first, *rest = APP_MARKERS
        locator = self._page.locator(first)
        for marker in rest:
            locator = locator.or_(self._page.locator(marker))
        return locator

    async def _site_error(self) -> str:
        """The message the site itself put on screen, if any. It names the real
        problem — wrong password, locked account — instead of leaving us to
        guess from a timeout. A hidden banner reads as empty, as it should."""
        assert self._page is not None
        try:
            banner = self._page.locator(LOGIN_ERROR).first
            if await banner.count() == 0:
                return ""
            return (await banner.inner_text()).strip()
        except Exception as exc:
            log.warning("could not read the site's error banner: %s", exc)
            return ""

    async def _click_login_button(self) -> None:
        assert self._page is not None
        button = self._page.locator(LOGIN_SUBMIT).first
        if await button.count() == 0:
            await self._capture_failure("login_button_missing")
            raise BrokerError(
                f"could not find the login button ({LOGIN_SUBMIT}) — "
                "see the login_button_missing screenshot"
            )
        await button.click()

    async def login(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        # m.easytrader.ir is a mobile-only layout (bottom nav, buy/sell
        # buttons under the symbol page, etc.) — every selector in this file
        # was captured from a phone. A default desktop viewport/user-agent
        # would get served a different layout and none of them would match.
        device = self._playwright.devices["Pixel 5"]
        self._context = await self._browser.new_context(
            storage_state=self._load_storage_state(), **device
        )
        self._page = await self._context.new_page()

        # Real login happens even in dry-run: we need an authenticated page to
        # reach the order screen and take verification screenshots. Only the
        # final submit/confirm click in place_order() is skipped in dry-run.
        await self._page.goto(LOGIN_URL)

        password_field = self._page.locator(PASSWORD_INPUT)
        app_markers = self._app_markers()

        # Wait for the SPA (and any OAuth redirect) to settle into one of two
        # states before deciding anything. Checking straight after goto() reads
        # an empty page and wrongly concludes the saved session is still valid.
        try:
            await password_field.or_(app_markers).first.wait_for(
                state="visible", timeout=PAGE_READY_TIMEOUT_MS
            )
        except PlaywrightTimeoutError as exc:
            await self._capture_failure("login_stuck")
            raise BrokerError(
                "page never showed either the login form or the app — see the login_stuck screenshot"
            ) from exc

        await self._screenshot("login_page")

        # The password box is the login form's own marker: while it is on
        # screen no session was restored, so we have to sign in.
        if await password_field.is_visible():
            await self._type_into(self._page.locator(USERNAME_INPUT), self.username, "username")
            await self._type_into(password_field, self.password, "password")
            await self._screenshot("login_filled")

            await self._click_login_button()
            try:
                await app_markers.first.wait_for(state="visible", timeout=PAGE_READY_TIMEOUT_MS)
            except PlaywrightTimeoutError as exc:
                await self._capture_failure("login_failed")
                site_error = await self._site_error()
                if site_error:
                    log.error("the broker refused the login: %s", site_error)
                reason = (
                    f"the site says: {site_error}"
                    if site_error
                    else "wrong credentials, or an extra step (OTP/agreement) is in the way"
                )
                raise BrokerError(
                    f"login did not reach the app — {reason}. "
                    f"Still at: {self._page.url} — see the login_failed screenshot"
                ) from exc
            log.info("logged in with credentials as %s", self.username)
        else:
            log.info("restored saved session, skipping credential login for %s", self.username)

        await self._screenshot("login_done")
        await self._save_storage_state()

    async def place_order(self, order: Order) -> str:
        try:
            return await self._do_place_order(order)
        except BrokerError:
            await self._capture_failure("error")
            raise
        except Exception as exc:
            await self._capture_failure("error")
            raise BrokerError(f"unexpected error placing order: {exc!r}") from exc

    async def _check_ticket_symbol(self, expected: str) -> None:
        """The ticket that opened has to be for the symbol we asked for. The
        search matches on part of a name, so it can land on a neighbour — and
        a correctly sized order on the wrong symbol is the worst thing this bot
        could do. If the name cannot be read at all, that is also a stop: an
        unverifiable order is not worth sending."""
        assert self._page is not None
        header = self._page.locator(SYMBOL_HEADER).first
        if await header.count() == 0:
            await self._capture_failure("symbol_unverified")
            raise BrokerError(
                f"the open ticket does not say which symbol it is for, so it "
                f"cannot be confirmed as {expected} — "
                "not ordering on a ticket we cannot read"
            )

        shown = (await header.inner_text()).strip()
        if not _same_symbol(shown, expected):
            await self._capture_failure("wrong_symbol")
            raise BrokerError(
                f"the open ticket is for {shown}, not {expected} — "
                "the search landed on a different symbol, and nothing was ordered"
            )
        log.info("ticket confirmed for %s", shown)

    async def _first_match(self, root, selectors, what: str, hint: str = "") -> Locator:
        """The first of several shapes that actually matches, or a stop. Nothing
        is clicked on a guess: an order form is not the place for it."""
        for selector in selectors:
            candidate = root.locator(selector).first
            if await candidate.count() > 0:
                return candidate
        await self._capture_failure(f"missing_{what}")
        raise BrokerError(f"could not find the {what} on the order form{hint}")

    async def _open_keypad(self, field: Locator, what: str) -> Locator:
        """The box is readonly and driven by the app's own keypad, so it is
        opened the way a finger opens it — by tapping the box. The calculator
        and lock icons beside these boxes are other tools; they are left alone."""
        assert self._page is not None
        await field.click()
        try:
            await self._page.locator(KEYBOARD_OPEN).first.wait_for(
                state="attached", timeout=KEYPAD_OPEN_TIMEOUT_MS
            )
        except PlaywrightTimeoutError as exc:
            await self._capture_failure(f"{what}_keypad_missing")
            raise BrokerError(
                f"tapping the {what} box did not bring up the app's keypad, and "
                "the box itself is readonly — so the number cannot be entered at all"
            ) from exc

        await self._screenshot(f"{what}_keypad")
        # However this run ends, the keypad's markup is now on record.
        await self._save_page_html(KEYPAD_HTML_NAME)

        return await self._find_keypad()

    async def _holds_keys(self, candidate: Locator) -> bool:
        """Whether something is the keypad itself rather than one of its keys:
        a keypad has several digits inside it."""
        found = 0
        for digit in "0123456789":
            for selector in _digit_selectors(digit):
                if await candidate.locator(selector).first.count() > 0:
                    found += 1
                    break
            if found >= KEYPAD_MIN_KEYS:
                return True
        return False

    async def _find_keypad(self) -> Locator:
        """The keypad's markup has not been captured yet, so this tries the
        shapes it might have — and accepts only one that actually holds keys,
        since the patterns match a single key just as happily as the panel."""
        assert self._page is not None
        for selector in KEYPAD_CONTAINERS:
            candidates = await self._page.locator(selector).all()
            for candidate in candidates[:MAX_KEYPAD_CANDIDATES]:
                if await self._holds_keys(candidate):
                    return candidate

        await self._capture_failure("keypad_unrecognised")
        raise BrokerError(
            "the app's keypad opened but none of its keys were recognised — "
            f"{KEYPAD_HTML_NAME} in this run's folder has its markup, and "
            "nothing was clicked blindly"
        )

    async def _clear_number(self, field: Locator, keypad: Locator, what: str) -> None:
        """The box opens pre-filled from the account's buying power, so it holds
        an arbitrary number. Backspace it away key by key — there is no other way
        into a readonly box — and stop the order if it will not empty."""
        backspace = await self._first_match(
            keypad, KEYPAD_BACKSPACE, "keypad backspace key",
            hint=f" — see {KEYPAD_HTML_NAME} in this run's folder",
        )
        for _ in range(MAX_CLEAR_PRESSES):
            if not _digits(await field.input_value()):
                return
            await backspace.click()

        await self._capture_failure(f"{what}_not_cleared")
        raise BrokerError(
            f"the {what} box would not clear — it still reads "
            f"{_digits(await field.input_value())}; not sending an order for an "
            "amount we did not choose"
        )

    async def _set_number(self, selector: str, value: int, what: str) -> None:
        """Put one number into one of the order form's readonly boxes, and be
        sure it landed: both mistakes possible here — a leftover digit, or a key
        that entered something else — would send a live order for the wrong
        amount."""
        assert self._page is not None
        field = await self._first_match(self._page, (selector,), f"{what} box")

        keypad = await self._open_keypad(field, what)
        await self._clear_number(field, keypad, what)
        for digit in str(value):
            key = await self._first_match(
                keypad, _digit_selectors(digit), f"keypad key {digit}",
                hint=f" — see {KEYPAD_HTML_NAME} in this run's folder",
            )
            await key.click()

        await self._screenshot(f"{what}_filled")

        entered = _digits(await field.input_value())
        if entered != str(value):
            await self._capture_failure(f"{what}_wrong")
            raise BrokerError(
                f"the {what} box reads {entered or 'nothing'} after entering {value} — "
                "not sending an order for the wrong amount"
            )

    async def _do_place_order(self, order: Order) -> str:
        assert self._page is not None
        page = self._page

        await self._screenshot("landing")
        await page.locator(NAVBAR_SEARCH).first.click()
        await page.get_by_placeholder(SYMBOL_SEARCH_PLACEHOLDER).fill(order.symbol)
        await self._screenshot("search")
        await page.get_by_text(order.symbol, exact=False).first.click()
        await self._screenshot("symbol_page")

        side_button_text = BUY_BUTTON_TEXT if order.side == Side.BUY else SELL_BUTTON_TEXT
        await page.get_by_text(side_button_text, exact=True).first.click()
        await self._screenshot("ticket_opened")
        await self._check_ticket_symbol(order.symbol)

        await self._set_number(QUANTITY_INPUT, order.quantity, "quantity")
        if order.order_type == OrderType.LIMIT:
            # Market orders leave the price box on its pre-filled last-trade
            # price; only override it for an explicit limit price.
            await self._set_number(PRICE_INPUT, order.price, "price")
        await self._screenshot("form_filled")

        if self.dry_run:
            log.info(
                "[dry-run] form filled but NOT submitted: %s %s x%s (%s%s) — see screenshots",
                order.side.value,
                order.symbol,
                order.quantity,
                order.order_type.value,
                f" @ {order.price}" if order.order_type == OrderType.LIMIT else "",
            )
            return f"dry-run-{order.id}"

        # There is no confirmation step at this broker: the send button places
        # the order, so by this point it is already live.
        submit = await self._first_match(
            page, _submit_selectors(order.side), f"{order.side.value} send button"
        )
        await submit.click()
        await self._screenshot("after_submit")

        await page.wait_for_timeout(SUBMIT_SETTLE_MS)
        await self._screenshot("after_submit_1s")

        try:
            await page.get_by_text(SUCCESS_TEXT).first.wait_for(timeout=SUCCESS_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            # Nothing here says the order failed — only that we did not
            # recognise the reply. SUCCESS_TEXT is still a guess, so keep the
            # markup: it carries the wording the site actually uses.
            await self._capture_failure("no_confirmation")
            raise BrokerError(
                f"order was sent, but no message matching {SUCCESS_TEXT!r} appeared — "
                "it may well have gone through. Read the site's real wording out of "
                f"{PAGE_HTML_NAME} in that run's folder (and the no_confirmation "
                "screenshot for what was on screen), then correct SUCCESS_TEXT"
            ) from exc

        return f"submitted-{order.id}"

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
