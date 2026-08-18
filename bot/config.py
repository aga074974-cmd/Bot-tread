from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from bot.models import Order, OrderType, Side

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


@dataclass
class Settings:
    base_url: str
    username: str
    password: str
    dry_run: bool
    grace_period_seconds: int
    max_retries: int
    retry_delay_seconds: float

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        dry_run_raw = os.getenv("DRY_RUN", "true").strip().lower()
        return cls(
            base_url=os.getenv("MOFID_BASE_URL", ""),
            username=os.getenv("MOFID_USERNAME", ""),
            password=os.getenv("MOFID_PASSWORD", ""),
            dry_run=dry_run_raw not in ("false", "0", "no"),
            grace_period_seconds=int(os.getenv("GRACE_PERIOD_SECONDS", "120")),
            max_retries=int(os.getenv("MAX_RETRIES", "5")),
            retry_delay_seconds=float(os.getenv("RETRY_DELAY_SECONDS", "2")),
        )


def _parse_datetime(value: str) -> datetime:
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S") if len(value) > 16 else datetime.strptime(value, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TEHRAN_TZ)


def load_orders(path: str | Path) -> list[Order]:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    orders: list[Order] = []
    for item in data.get("orders", []):
        orders.append(
            Order(
                symbol=item["symbol"],
                symbol_id=item.get("symbol_id"),
                side=Side(item["side"]),
                quantity=int(item["quantity"]),
                order_type=OrderType(item.get("order_type", "market")),
                price=item.get("price"),
                scheduled_at=_parse_datetime(str(item["at"])),
            )
        )
    return orders
