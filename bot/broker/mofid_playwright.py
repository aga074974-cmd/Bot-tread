from __future__ import annotations

import json
import logging
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

# Whether the app is up is decided on several of its labels rather than one:
# any single one could be renamed or A/B tested without the app being gone.
APP_MARKERS = ("دیده‌بان", "جستجو", "قدرت خرید")

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
# Filled in from real screenshots of the order ticket, except where noted.
# The quantity/price fields *look* like placeholders in the screenshot but
# could turn out to be separate floating labels once tested live — if
# place_order() fails, check that run's folder under logs/screenshots/ to see
# exactly how far it got.
# ---------------------------------------------------------------------------
SEARCH_TAB_TEXT = "جستجو"  # bottom nav tab
SYMBOL_SEARCH_PLACEHOLDER = "جستجوی نماد"
BUY_BUTTON_TEXT = "خرید"
SELL_BUTTON_TEXT = "فروش"
QUANTITY_PLACEHOLDER = "تعداد"
PRICE_PLACEHOLDER = "قیمت"
SUCCESS_TEXT = "با موفقیت"  # unverified — not seen in captured screenshots yet


def _submit_button_text(side: Side) -> str:
    return "ارسال خرید" if side == Side.BUY else "ارسال فروش"


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

    async def _save_page_html(self) -> None:
        """Dump the live DOM next to that run's screenshots. The screenshot
        shows where a run stopped; the markup shows why — which is what you
        need to retune a selector that no longer matches."""
        if not self._shot_dir or not self._page:
            return
        self._shot_dir.mkdir(parents=True, exist_ok=True)
        path = self._shot_dir / PAGE_HTML_NAME
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
        locator = self._page.get_by_text(first)
        for marker in rest:
            locator = locator.or_(self._page.get_by_text(marker))
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

    async def _do_place_order(self, order: Order) -> str:
        assert self._page is not None
        page = self._page

        await self._screenshot("landing")
        await page.get_by_text(SEARCH_TAB_TEXT, exact=True).first.click()
        await page.get_by_placeholder(SYMBOL_SEARCH_PLACEHOLDER).fill(order.symbol)
        await self._screenshot("search")
        await page.get_by_text(order.symbol, exact=False).first.click()
        await self._screenshot("symbol_page")

        side_button_text = BUY_BUTTON_TEXT if order.side == Side.BUY else SELL_BUTTON_TEXT
        await page.get_by_text(side_button_text, exact=True).first.click()
        await self._screenshot("ticket_opened")

        await page.get_by_placeholder(QUANTITY_PLACEHOLDER).fill(str(order.quantity))
        if order.order_type == OrderType.LIMIT:
            # Market orders leave the price field at its pre-filled last-trade
            # price; only override it for an explicit limit price.
            await page.get_by_placeholder(PRICE_PLACEHOLDER).fill(str(order.price))
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
        await page.get_by_text(_submit_button_text(order.side), exact=True).first.click()
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
