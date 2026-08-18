from __future__ import annotations

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
# NOT FILLED IN YET — order-screen selectors are guesses until we see real
# screenshots of the order ticket. Update these before using DRY_RUN=false.
# ---------------------------------------------------------------------------
SYMBOL_SEARCH_PLACEHOLDER = "جستجو"
BUY_TAB_TEXT = "خرید"
SELL_TAB_TEXT = "فروش"
QUANTITY_PLACEHOLDER = "تعداد"
PRICE_PLACEHOLDER = "قیمت"
SUBMIT_BUTTON_TEXT = "ارسال سفارش"
CONFIRM_BUTTON_TEXT = "تایید"
SUCCESS_TEXT = "با موفقیت"


class MofidPlaywrightClient(BrokerClient):
    def __init__(
        self,
        username: str,
        password: str,
        dry_run: bool = True,
        headless: bool = True,
        debug_screenshot_dir: str | None = "logs/screenshots",
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

    async def login(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()

        # Real login happens even in dry-run: we need an authenticated page to
        # reach the order screen and take verification screenshots. Only the
        # final submit/confirm click in place_order() is skipped in dry-run.
        await self._page.goto(LOGIN_URL)
        await self._screenshot("login_page")
        await self._page.get_by_placeholder(USERNAME_PLACEHOLDER).fill(self.username)
        await self._page.get_by_placeholder(PASSWORD_PLACEHOLDER).fill(self.password)
        await self._screenshot("login_filled")
        await self._page.get_by_text(LOGIN_BUTTON_TEXT, exact=True).click()
        await self._page.wait_for_url(f"{LOGIN_URL}**", timeout=30_000)
        await self._screenshot("login_done")
        log.info("logged in as %s", self.username)

    async def place_order(self, order: Order) -> str:
        assert self._page is not None
        page = self._page

        # TODO: verify these steps against the real order screen.
        await page.get_by_placeholder(SYMBOL_SEARCH_PLACEHOLDER).fill(order.symbol)
        await self._screenshot(f"{order.id}_search")
        await page.get_by_text(order.symbol, exact=False).first.click()
        await self._screenshot(f"{order.id}_symbol_page")

        tab_text = BUY_TAB_TEXT if order.side == Side.BUY else SELL_TAB_TEXT
        await page.get_by_text(tab_text, exact=True).click()

        await page.get_by_placeholder(QUANTITY_PLACEHOLDER).fill(str(order.quantity))
        if order.order_type == OrderType.LIMIT:
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

        await page.get_by_text(SUBMIT_BUTTON_TEXT, exact=True).click()
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
