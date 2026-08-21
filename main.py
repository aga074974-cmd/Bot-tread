from __future__ import annotations

import argparse
import asyncio
import logging

from bot.broker.mofid_playwright import MofidPlaywrightClient
from bot.config import Settings, load_orders
from bot.logging_setup import setup_logging
from bot.models import Order
from bot.scheduler import run_all
from bot.screenshots import BASE_DIR, purge_old, run_dir

log = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduled buy/sell orders for Mofid EasyTrader")
    parser.add_argument("--orders", default="orders.yaml", help="path to the orders YAML file")
    args = parser.parse_args()

    setup_logging()
    settings = Settings.load()
    orders = load_orders(args.orders)

    if settings.dry_run:
        log.warning("DRY_RUN is enabled: no real orders will be sent. Set DRY_RUN=false in .env to go live.")

    purge_old(retention_days=settings.screenshot_retention_days)

    def broker_factory(order: Order) -> MofidPlaywrightClient:
        return MofidPlaywrightClient(
            username=settings.username,
            password=settings.password,
            dry_run=settings.dry_run,
            headless=settings.headless,
            storage_state_path=settings.storage_state_path,
            screenshot_dir=run_dir(BASE_DIR, f"{order.symbol}_{order.id}"),
        )

    await run_all(
        broker_factory,
        orders,
        grace_period_seconds=settings.grace_period_seconds,
        max_retries=settings.max_retries,
        retry_delay_seconds=settings.retry_delay_seconds,
        retry_deadline_seconds=settings.retry_deadline_seconds,
    )


if __name__ == "__main__":
    asyncio.run(main())
