from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from bot.broker.base import BrokerClient, BrokerError
from bot.models import Order, OrderType, Side

log = logging.getLogger(__name__)

LOGIN_URL = "https://m.easytrader.ir/"
USERNAME_PLACEHOLDER = "کدملی/شماره‌همراه/شناسه‌ملی/نام‌کاربری"
PASSWORD_PLACEHOLDER = "کلمه عبور"
LOGIN_BUTTON_TEXT = "ورود"

# ---------------------------------------------------------------------------
# Filled in from real screenshots of the order ticket, except where noted.
# The quantity/price fields *look* like placeholders in the screenshot but
# could turn out to be separate floating labels once tested live — if
# place_order() fails, check logs/screenshots/<id>_form_filled (or the
# _error shot) to see exactly how far it got.
# ---------------------------------------------------------------------------
SEARCH_TAB_TEXT = "جستجو"  # bottom nav tab that reveals the symbol search box
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
        debug_screenshot_dir: str | None = "logs/screenshots",
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
        self._shot_dir = Path(debug_screenshot_dir) if debug_screenshot_dir else None
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

    async def login(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(storage_state=self._load_storage_state())
        self._page = await self._context.new_page()

        # Real login happens even in dry-run: we need an authenticated page to
        # reach the order screen and take verification screenshots. Only the
        # final submit/confirm click in place_order() is skipped in dry-run.
        await self._page.goto(LOGIN_URL)
        await self._screenshot("login_page")

        username_field = self._page.get_by_placeholder(USERNAME_PLACEHOLDER)
        if await username_field.count() == 0:
            log.info("restored saved session, skipping credential login for %s", self.username)
        else:
            await username_field.fill(self.username)
            await self._page.get_by_placeholder(PASSWORD_PLACEHOLDER).fill(self.password)
            await self._screenshot("login_filled")
            await self._page.get_by_text(LOGIN_BUTTON_TEXT, exact=True).click()
            await self._page.wait_for_url(f"{LOGIN_URL}**", timeout=30_000)
            log.info("logged in with credentials as %s", self.username)

        await self._screenshot("login_done")
        await self._save_storage_state()

    async def place_order(self, order: Order) -> str:
        try:
            return await self._do_place_order(order)
        except BrokerError:
            await self._screenshot(f"{order.id}_error")
            raise
        except Exception as exc:
            await self._screenshot(f"{order.id}_error")
            raise BrokerError(f"unexpected error placing order: {exc!r}") from exc

    async def _do_place_order(self, order: Order) -> str:
        assert self._page is not None
        page = self._page

        await self._screenshot(f"{order.id}_landing")
        await page.get_by_text(SEARCH_TAB_TEXT, exact=True).click()
        await page.get_by_placeholder(SYMBOL_SEARCH_PLACEHOLDER).fill(order.symbol)
        await self._screenshot(f"{order.id}_search")
        await page.get_by_text(order.symbol, exact=False).first.click()
        await self._screenshot(f"{order.id}_symbol_page")

        side_button_text = BUY_BUTTON_TEXT if order.side == Side.BUY else SELL_BUTTON_TEXT
        await page.get_by_text(side_button_text, exact=True).click()
        await self._screenshot(f"{order.id}_ticket_opened")

        await page.get_by_placeholder(QUANTITY_PLACEHOLDER).fill(str(order.quantity))
        if order.order_type == OrderType.LIMIT:
            # Market orders leave the price field at its pre-filled last-trade
            # price; only override it for an explicit limit price.
            await page.get_by_placeholder(PRICE_PLACEHOLDER).fill(str(order.price))
        await self._screenshot(f"{order.id}_form_filled")

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

        await page.get_by_text(_submit_button_text(order.side), exact=True).click()
        await self._screenshot(f"{order.id}_after_submit")

        confirm_btn = page.get_by_text(CONFIRM_BUTTON_TEXT, exact=True)
        if await confirm_btn.count() > 0:
            await confirm_btn.click()
            await self._screenshot(f"{order.id}_after_confirm")

        try:
            await page.get_by_text(SUCCESS_TEXT).wait_for(timeout=10_000)
        except Exception as exc:
            await self._screenshot(f"{order.id}_error")
            raise BrokerError(f"no success confirmation seen after submitting order: {exc}") from exc

        return f"submitted-{order.id}"

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
