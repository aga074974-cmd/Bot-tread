from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright, Browser, BrowserContext, Locator, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from bot.broker.base import BrokerClient, BrokerError, OrderRefused
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

# Inside the app the messages come from a different place: an <app-notify>
# host holding one .notify__item per message — the rose pill with the cross
# reading e.g. "محدوده زمانی سفارش معتبر نمی‌باشد!". Read off a captured order
# page, which carries the host and the CSS but no live item: these are toasts,
# gone again in a few seconds, which is why the page dump taken fifteen
# seconds after a refusal has no trace of the refusal in it.
ORDER_NOTICE = "app-notify .notify__item"
# Trimmed off the end of a message: the toast's own dismiss control, for
# the case it is a character and not an icon. Only shapes no Persian
# sentence ends in — never a letter or a digit.
CLOSE_GLYPHS = "✕✖×✗⨯ \t\u200c"

# Some symbols — leveraged fund units among them — will not trade until the
# account holder has personally signed a risk acknowledgement. The app puts it
# up as a bottom sheet with موافقت می‌نمایم / انصراف and refuses to go on
# without an answer. Read off a captured page: one <ui-bottom-sheet>, its
# panel carrying .show while it is up, and the wording in .modal-title.
#
# The bot never answers it. Signing a declaration that says the holder
# understands the risk is the holder's to do, not a robot's, so this is only
# ever recognised and reported.
CONSENT_SHEET = "ui-bottom-sheet .bottom-sheet.show"
CONSENT_TITLE = ".modal-title"
# Matched on the two words that name the document rather than the whole
# heading, which carries the fund's name and changes with it.
CONSENT_WORDS = re.compile(r"اقرارنامه|بیانیه ریسک")
CONSENT_MARK = "needs the fund's risk acknowledgement"

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
# on the spot catches the form mid-flight. A second shot is taken at the
# midpoint below and a third once the full settle time has passed, so the
# reaction is on record whichever of the two it shows up in.
SUBMIT_MIDPOINT_MS = 500
SUBMIT_SETTLE_MS = 1_000

# Said when the broker's own servers are the problem — nginx answering with a
# 5xx page instead of the app. Named rather than left to time out, because
# "the page never showed the login form" sends someone hunting for a selector
# that changed when nothing changed: the site is simply down.
SITE_DOWN = "the broker site is unavailable"

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

# Confirmed from a captured keypad.html: the app's numeric keypad is a
# <ui-keyboard> element, its digit keys are data-cy="easy-keyboard-btn-N", and
# a single press of easy-keyboard-btn-deleteAll empties the box in one go
# (easy-keyboard-btn-delete is the plain one-digit backspace next to it). The
# rest are kept as fallbacks — a different box (price vs quantity) or a future
# redesign is not guaranteed to reuse the exact same component. Every run
# saves KEYPAD_HTML_NAME while the keypad is open, and if nothing here
# matches, the run stops: on an order form, clicking something we have not
# identified is worse than not ordering at all.
KEYPAD_CONTAINERS = (
    "ui-keyboard",
    '[data-cy*="keyboard"]',
    '[data-cy*="keypad"]',
    "app-keyboard, keyboard-widget, numeric-keyboard",
    '[class*="keyboard"]:not(#root):not(html):not(body)',
    '[class*="keypad"]',
    '[id*="keyboard"]:not(#root)',
)

# A single press that clears the whole box — tried first, since it is a
# stronger guarantee of "completely empty" than any number of backspaces.
KEYPAD_CLEAR_ALL = (
    '[data-cy="easy-keyboard-btn-deleteAll"]',
    '[data-cy*="deleteAll"]',
    '[data-cy*="clearAll"]',
    ':text-is("پاک کردن")',
)
KEYPAD_BACKSPACE = (
    '[data-cy="easy-keyboard-btn-delete"]',
    '[data-cy*="backspace"]',
    '[data-cy*="delete"]',
    '[class*="backspace"]',
    '[class*="delete"]',
    ':text-is("⌫")',
    ':text-is("حذف")',
)


def _digit_selectors(digit: str) -> tuple[str, ...]:
    """Ways one key of the keypad might be written, best first."""
    persian = "۰۱۲۳۴۵۶۷۸۹"[int(digit)]
    return (
        f'[data-cy="easy-keyboard-btn-{digit}"]',
        f'[data-cy="keyboard-key-{digit}"]',
        f'[data-cy$="key-{digit}"]',
        f'[data-cy$="btn-{digit}"]',
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
        on_login_result: Callable[[bool, str], Awaitable[None]] | None = None,
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
        self._on_login_result = on_login_result

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

    async def _site_error(self, selector: str = LOGIN_ERROR) -> str:
        """The message the site itself put on screen, if any. It names the real
        problem — wrong password, locked account — instead of leaving us to
        guess from a timeout. A hidden banner reads as empty, as it should."""
        assert self._page is not None
        try:
            banner = self._page.locator(selector).first
            if await banner.count() == 0:
                return ""
            # The toast reads as one line here even though it is laid out over
            # several, and its close control comes along as a stray glyph when
            # the app draws it as a character rather than an icon.
            return " ".join((await banner.inner_text()).split()).strip(CLOSE_GLYPHS).strip()
        except Exception as exc:
            log.warning("could not read the site's error banner: %s", exc)
            return ""

    async def _consent_wanted(self) -> str:
        """The risk acknowledgement's own heading if the app is asking for it,
        "" otherwise. Nothing this bot does can answer it — see CONSENT_SHEET."""
        assert self._page is not None
        try:
            sheet = self._page.locator(CONSENT_SHEET).first
            if await sheet.count() == 0 or not await sheet.is_visible():
                return ""
            title = sheet.locator(CONSENT_TITLE).first
            heading = " ".join((await title.inner_text()).split()) if await title.count() else ""
            return heading if CONSENT_WORDS.search(heading) else ""
        except Exception as exc:
            log.warning("could not read the consent sheet: %s", exc)
            return ""

    async def _refuse_if_consent_wanted(self, when: str) -> None:
        """Deliberately not phrased as "the site says": the panel hands that
        wording straight to whoever is reading, and this sheet's heading is a
        line of legal title, not something that tells them what to do."""
        heading = await self._consent_wanted()
        if not heading:
            return
        await self._capture_failure(f"consent_wanted_{when}")
        await self._save_page_html()
        raise OrderRefused(f"{CONSENT_MARK}: {heading}")

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

    async def _report_login_result(self, success: bool, detail: str = "") -> None:
        """Tells whoever is tracking session health (the panel's status light
        and its consecutive-failure counter) how a login attempt went. A bug
        in that reporting must never turn a real login result into a
        different one, so it is never allowed to raise."""
        if self._on_login_result is None:
            return
        try:
            await self._on_login_result(success, detail)
        except Exception:
            log.exception("on_login_result callback raised, ignoring")

    async def _goto_app(self) -> None:
        """Load the site, and say plainly when the site itself is down. Left to
        the wait below, a 5xx page from nginx times out like everything else
        and gets reported as the app not appearing, which reads as a broken
        selector and sends someone looking for a change that never happened."""
        assert self._page is not None
        try:
            response = await self._page.goto(LOGIN_URL)
        except PlaywrightError as exc:
            # Never reached the server at all: DNS, refused connection, the
            # server's own network. Same conclusion from where we stand.
            raise BrokerError(f"{SITE_DOWN} — {LOGIN_URL} did not answer ({exc})") from exc
        if response is not None and response.status >= 500:
            await self._capture_failure("site_unavailable")
            raise BrokerError(f"{SITE_DOWN} — {LOGIN_URL} answered {response.status}")

    async def _open_session(self) -> Locator:
        """Launch a browser, load the saved session if any, and wait for the
        page to settle into one of two states: the login form, or the app
        itself. Shared by login() (which signs in when the form is what
        showed up) and check_session() (which only ever reads this same
        signal — it must never attempt a credential login on its own). The
        password field locator is returned so callers tell the two states
        apart with is_visible()."""
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
        await self._goto_app()

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

        return password_field

    async def _do_login(self) -> None:
        # Real login happens even in dry-run: we need an authenticated page to
        # reach the order screen and take verification screenshots. Only the
        # final submit/confirm click in place_order() is skipped in dry-run.
        password_field = await self._open_session()

        # The password box is the login form's own marker: while it is on
        # screen no session was restored, so we have to sign in.
        if await password_field.is_visible():
            await self._type_into(self._page.locator(USERNAME_INPUT), self.username, "username")
            await self._type_into(password_field, self.password, "password")
            await self._screenshot("login_filled")

            await self._click_login_button()
            try:
                await self._app_markers().first.wait_for(state="visible", timeout=PAGE_READY_TIMEOUT_MS)
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

        await self._save_storage_state()

    async def login(self) -> None:
        try:
            await self._do_login()
        except BrokerError as exc:
            await self._report_login_result(False, str(exc))
            raise
        except Exception as exc:
            await self._report_login_result(False, repr(exc))
            raise BrokerError(f"unexpected error logging in: {exc!r}") from exc
        else:
            await self._report_login_result(True)

    async def check_session(self) -> bool:
        """Whether the saved session (auth_state.json) is still good, read
        with exactly the signal login() itself uses — the login form's own
        visibility — but never typing credentials or clicking anything. This
        is a plain question for the panel's status light, not a login
        attempt, so unlike login() it always resolves to a bool rather than
        raising, and does not count toward the consecutive-failure streak."""
        try:
            password_field = await self._open_session()
        except Exception as exc:
            log.warning("session check inconclusive, treating the session as invalid: %r", exc)
            return False
        return not await password_field.is_visible()

    async def place_order(self, order: Order) -> str:
        try:
            return await self._do_place_order(order)
        except BrokerError:
            await self._save_page_html()
            raise
        except Exception as exc:
            await self._save_page_html()
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

    async def _try_match(self, root, selectors) -> Locator | None:
        """The first of several shapes that actually matches, or None — for a
        shape that has a fallback if this one turns out not to be there."""
        for selector in selectors:
            candidate = root.locator(selector).first
            if await candidate.count() > 0:
                return candidate
        return None

    async def _first_match(self, root, selectors, what: str, hint: str = "") -> Locator:
        """Like _try_match, but a miss stops the run. Nothing is clicked on a
        guess: an order form is not the place for it."""
        match = await self._try_match(root, selectors)
        if match is not None:
            return match
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

    @staticmethod
    def _is_empty(value: str) -> bool:
        """Empty, or the zero a number box falls back to when it is: both mean
        nothing of the pre-filled amount is left to be carried into the order."""
        digits = _digits(value)
        return digits == "" or int(digits) == 0

    async def _clear_number(self, field: Locator, keypad: Locator, what: str) -> None:
        """The box opens pre-filled from the account's buying power, so it holds
        an arbitrary number. The keypad's own "clear everything" key (پاک‌کردن,
        easy-keyboard-btn-deleteAll) is tried first — one press, fully empty.
        If that key cannot be found, fall back to backspacing it away one digit
        at a time — there is no other way into a readonly box. Either way, stop
        the order if it will not empty."""
        clear_all = await self._try_match(keypad, KEYPAD_CLEAR_ALL)
        if clear_all is not None:
            await clear_all.click()
            if self._is_empty(await field.input_value()):
                log.info("%s box cleared with the keypad's پاک‌کردن key", what)
                return

        backspace = await self._first_match(
            keypad, KEYPAD_BACKSPACE, "keypad backspace key",
            hint=f" — see {KEYPAD_HTML_NAME} in this run's folder",
        )
        for _ in range(MAX_CLEAR_PRESSES):
            if self._is_empty(await field.input_value()):
                log.info("%s box cleared before entering the order's own number", what)
                return
            await backspace.click()

        await self._capture_failure(f"{what}_not_cleared")
        raise BrokerError(
            f"the {what} box would not clear — it still reads "
            f"{_digits(await field.input_value())}; not sending an order for an "
            "amount we did not choose"
        )

    async def _enter_number(self, selector: str, value: int, what: str) -> None:
        """Put one number into one of the order form's readonly boxes: open the
        app's keypad, clear whatever the box came up with, and press the digits.
        Photographed the moment the last digit lands — checking it comes after,
        so nothing sits between the number arriving and the picture of it."""
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

    async def _check_number(self, selector: str, value: int, what: str) -> None:
        """Read the box back and refuse to send unless it holds exactly the
        number asked for. Compared as a number, not as text: a box that fell
        back to 0 before the digits reads 080 for 80, which is the same amount
        and no reason to abandon a good order."""
        assert self._page is not None
        entered = _digits(await self._page.locator(selector).first.input_value())
        if not entered or int(entered) != value:
            await self._capture_failure(f"{what}_wrong")
            raise BrokerError(
                f"the {what} box reads {entered or 'nothing'} after entering {value} — "
                "not sending an order for the wrong amount"
            )
        log.info("%s box confirmed at %s", what, value)

    async def _do_place_order(self, order: Order) -> str:
        assert self._page is not None
        page = self._page

        await self._screenshot("landing")
        await page.locator(NAVBAR_SEARCH).first.click()
        await page.get_by_placeholder(SYMBOL_SEARCH_PLACEHOLDER).fill(order.symbol)
        await self._screenshot("search")
        await page.get_by_text(order.symbol, exact=False).first.click()

        side_button_text = BUY_BUTTON_TEXT if order.side == Side.BUY else SELL_BUTTON_TEXT
        await page.get_by_text(side_button_text, exact=True).first.click()
        await self._screenshot("ticket_opened")
        # Up before anything is filled in, it covers the form: catch it here
        # rather than let it surface as a keypad that would not open.
        await self._refuse_if_consent_wanted("on_ticket")
        await self._check_ticket_symbol(order.symbol)

        await self._enter_number(QUANTITY_INPUT, order.quantity, "quantity")
        if order.order_type == OrderType.LIMIT:
            # Market orders leave the price box on its pre-filled last-trade
            # price; only override it for an explicit limit price.
            await self._enter_number(PRICE_INPUT, order.price, "price")

        # Read back before anything is sent, which is the only place the
        # check has to be. _enter_number() already photographed each box
        # right as its own digits landed.
        await self._check_number(QUANTITY_INPUT, order.quantity, "quantity")
        if order.order_type == OrderType.LIMIT:
            await self._check_number(PRICE_INPUT, order.price, "price")

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

        # However this run ends, a shot at the half-second mark is not
        # optional: the reaction to a live order is worth having even if
        # everything after this point goes wrong.
        await page.wait_for_timeout(SUBMIT_MIDPOINT_MS)
        await self._screenshot("after_submit_500ms")

        await page.wait_for_timeout(SUBMIT_SETTLE_MS - SUBMIT_MIDPOINT_MS)
        await self._screenshot("after_submit_1s")

        # A sent order comes back one of two ways: the success message, or the
        # app's own red banner naming the reason it was refused — outside
        # trading hours, not enough buying power. Wait for whichever arrives
        # first. Waiting only on success meant a refusal sat out the whole
        # timeout and was then reported as "sent, but the reply was not
        # recognised", which is the opposite of what happened to the order.
        success = page.get_by_text(SUCCESS_TEXT).first
        notice = page.locator(ORDER_NOTICE).first
        consent = page.locator(CONSENT_SHEET).first
        try:
            await success.or_(notice).or_(consent).first.wait_for(
                state="visible", timeout=SUCCESS_TIMEOUT_MS
            )
        except PlaywrightTimeoutError as exc:
            # Neither one showed. Nothing here says the order failed — only
            # that we did not recognise the reply. SUCCESS_TEXT is still a
            # guess, so keep the markup: it carries the site's real wording.
            await self._save_page_html()
            raise BrokerError(
                f"order was sent, but no message matching {SUCCESS_TEXT!r} appeared — "
                "it may well have gone through. Read the site's real wording out of "
                f"{PAGE_HTML_NAME} in that run's folder, then correct SUCCESS_TEXT"
            ) from exc

        # Asked for in answer to the send click: the app is gating the order on
        # a signature, so it did not go through.
        await self._refuse_if_consent_wanted("on_submit")

        said = await self._site_error(ORDER_NOTICE)
        # A confirmation arrives through this same toast, in a different
        # colour, so one carrying SUCCESS_TEXT is not a refusal — whichever of
        # the two locators woke the wait above. Getting this backwards is the
        # expensive direction: OrderRefused is final, but every other failure
        # is retried, so calling a successful order a plain failure is what
        # would send it a second time.
        if said and SUCCESS_TEXT not in said:
            await self._capture_failure("order_refused")
            await self._save_page_html()
            raise OrderRefused(f"the order was refused — the site says: {said}")

        return f"submitted-{order.id}"

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
