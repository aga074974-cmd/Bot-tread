from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from bot.broker.base import BrokerClient, BrokerError
from bot.models import Order, OrderType, Side

log = logging.getLogger(__name__)

LOGIN_URL = "https://m.easytrader.ir/"
USERNAME_PLACEHOLDER = "کدملی/شماره‌همراه/شناسه‌ملی/نام‌کاربری"
PASSWORD_PLACEHOLDER = "کلمه عبور"
LOGIN_BUTTON_TEXT = "ورود"

# The site is an Angular SPA behind an OAuth redirect, so nothing is on the
# page at load time. Every step waits for its element rather than assuming
# it's already there.
PAGE_READY_TIMEOUT_MS = 45_000

# ---------------------------------------------------------------------------
# Filled in from real screenshots of the order ticket, except where noted.
# The quantity/price fields *look* like placeholders in the screenshot but
# could turn out to be separate floating labels once tested live — if
# place_order() fails, check that run's folder under logs/screenshots/ to see
# exactly how far it got.
# ---------------------------------------------------------------------------
SEARCH_TAB_TEXT = "جستجو"  # bottom nav tab; also our "we're logged in" marker
SYMBOL_SEARCH_PLACEHOLDER = "جستجوی نماد"
BUY_BUTTON_TEXT = "خرید"
SELL_BUTTON_TEXT = "فروش"
QUANTITY_PLACEHOLDER = "تعداد"
PRICE_PLACEHOLDER = "قیمت"
CONFIRM_BUTTON_TEXT = "تایید"
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
            await self._screenshot("login_input_rejected")
            raise BrokerError(
                f"could not enter the {what} — the field rejected both typing and fill(); "
                "see the login_input_rejected screenshot"
            )

    async def _click_login_button(self) -> None:
        """The submit control may be a <button>, or a styled div/anchor. Try the
        accessible role first, then fall back to plain text."""
        assert self._page is not None
        by_role = self._page.get_by_role("button", name=LOGIN_BUTTON_TEXT, exact=True)
        if await by_role.count() > 0:
            await by_role.first.click()
            return

        by_text = self._page.get_by_text(LOGIN_BUTTON_TEXT, exact=True)
        if await by_text.count() > 0:
            log.info("login button matched by text, not by button role")
            await by_text.first.click()
            return

        await self._screenshot("login_button_missing")
        raise BrokerError(
            f"could not find the '{LOGIN_BUTTON_TEXT}' button — see the login_button_missing screenshot"
        )

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

        username_field = self._page.get_by_placeholder(USERNAME_PLACEHOLDER)
        app_shell = self._page.get_by_text(SEARCH_TAB_TEXT, exact=True).first

        # Wait for the SPA (and any OAuth redirect) to settle into one of two
        # states before deciding anything. Checking straight after goto() reads
        # an empty page and wrongly concludes the saved session is still valid.
        try:
            await username_field.or_(app_shell).first.wait_for(
                state="visible", timeout=PAGE_READY_TIMEOUT_MS
            )
        except PlaywrightTimeoutError as exc:
            await self._screenshot("login_stuck")
            raise BrokerError(
                "page never showed either the login form or the app — see the login_stuck screenshot"
            ) from exc

        await self._screenshot("login_page")

        if await username_field.is_visible():
            await self._type_into(username_field, self.username, "username")
            password_field = self._page.get_by_placeholder(PASSWORD_PLACEHOLDER)
            await self._type_into(password_field, self.password, "password")
            await self._screenshot("login_filled")

            await self._click_login_button()
            try:
                await app_shell.wait_for(state="visible", timeout=PAGE_READY_TIMEOUT_MS)
            except PlaywrightTimeoutError as exc:
                await self._screenshot("login_failed")
                raise BrokerError(
                    "login did not reach the app — wrong credentials, or an extra step "
                    f"(OTP/agreement) is in the way. Still at: {self._page.url} — "
                    "see the login_failed screenshot"
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
            await self._screenshot("error")
            raise
        except Exception as exc:
            await self._screenshot("error")
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

        await page.get_by_text(_submit_button_text(order.side), exact=True).first.click()
        await self._screenshot("after_submit")

        confirm_btn = page.get_by_text(CONFIRM_BUTTON_TEXT, exact=True).first
        if await confirm_btn.count() > 0:
            await confirm_btn.click()
            await self._screenshot("after_confirm")

        try:
            await page.get_by_text(SUCCESS_TEXT).first.wait_for(timeout=15_000)
        except PlaywrightTimeoutError as exc:
            await self._screenshot("no_confirmation")
            raise BrokerError(
                "no success confirmation seen after submitting — check the "
                "no_confirmation screenshot to see whether it actually went through"
            ) from exc

        return f"submitted-{order.id}"

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
