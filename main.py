from __future__ import annotations

import argparse
import asyncio
import logging

from bot.broker.mofid import MofidClient
from bot.config import Settings, load_orders
from bot.logging_setup import setup_logging
from bot.scheduler import run_all

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

    broker = MofidClient(
        base_url=settings.base_url,
        username=settings.username,
        password=settings.password,
        dry_run=settings.dry_run,
    )
    await run_all(
        broker,
        orders,
        grace_period_seconds=settings.grace_period_seconds,
        max_retries=settings.max_retries,
        retry_delay_seconds=settings.retry_delay_seconds,
    )


if __name__ == "__main__":
    asyncio.run(main())
