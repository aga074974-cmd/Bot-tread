from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from bot.models import Order, OrderType, Side

log = logging.getLogger(__name__)

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

_FALSE_VALUES = ("false", "0", "no")
_TRUE_VALUES = ("true", "1", "yes")


def _parse_bool(name: str, default: str) -> bool:
    """A typo here (DRY_RUN=fasle) does not raise — it silently reads as
    "true", which for DRY_RUN means every order quietly fills the form and
    never sends it. Anything not recognised as true or false is logged, so
    that failure mode is loud instead of silent."""
    raw = os.getenv(name, default).strip().lower()
    if raw not in _FALSE_VALUES and raw not in _TRUE_VALUES:
        log.warning(
            "%s=%r is not true/false — treating it as %s. Check .env for a typo.",
            name, os.getenv(name), "true" if raw not in _FALSE_VALUES else "false",
        )
    return raw not in _FALSE_VALUES


@dataclass
class Settings:
    base_url: str
    username: str
    password: str
    dry_run: bool
    headless: bool
    grace_period_seconds: int
    max_retries: int
    retry_delay_seconds: float
    retry_deadline_seconds: int
    storage_state_path: str
    screenshot_retention_days: int
    order_history_retention_days: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        return cls(
            base_url=os.getenv("MOFID_BASE_URL", ""),
            username=os.getenv("MOFID_USERNAME", ""),
            password=os.getenv("MOFID_PASSWORD", ""),
            dry_run=_parse_bool("DRY_RUN", "true"),
            headless=_parse_bool("HEADLESS", "true"),
            grace_period_seconds=int(os.getenv("GRACE_PERIOD_SECONDS", "120")),
            max_retries=int(os.getenv("MAX_RETRIES", "5")),
            retry_delay_seconds=float(os.getenv("RETRY_DELAY_SECONDS", "2")),
            retry_deadline_seconds=int(os.getenv("RETRY_DEADLINE_SECONDS", "90")),
            storage_state_path=os.getenv("STORAGE_STATE_PATH", "auth_state.json"),
            screenshot_retention_days=int(os.getenv("SCREENSHOT_RETENTION_DAYS", "3")),
            order_history_retention_days=int(os.getenv("ORDER_HISTORY_RETENTION_DAYS", "30")),
        )


def update_env_values(path: str | Path, values: dict[str, str]) -> None:
    """Rewrite (or add) KEY=value lines in a .env file, leaving every other
    line untouched. Used only for credentials a person just typed into the
    panel's manual-login form and asked to keep — so this never logs what it
    writes, and the file is left owner-only afterward since it holds a
    password. Written to a temp file and swapped in with one atomic rename,
    so a crash mid-write cannot leave a half-written .env behind."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    updated_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                updated_lines.append(f"{key}={remaining.pop(key)}")
                continue
        updated_lines.append(line)
    for key, value in remaining.items():
        updated_lines.append(f"{key}={value}")

    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(path)


def parse_tehran_datetime(value: str) -> datetime:
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
                scheduled_at=parse_tehran_datetime(str(item["at"])),
            )
        )
    return orders
