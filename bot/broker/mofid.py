from __future__ import annotations

import logging

import httpx

from bot.broker.base import BrokerClient, BrokerError
from bot.models import Order, OrderType, Side

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NOT FILLED IN YET.
#
# This connector talks to Mofid's EasyTrader web app (m.easytrader.ir /
# login.emofid.com), which has no published API docs. The exact endpoints,
# auth flow and order payload below are placeholders — fill them in from a
# captured browser request (DevTools -> Network -> Copy as cURL) for:
#   1. the login request
#   2. an order-submission request
# Until then LOGIN_PATH/ORDER_PATH are unset and this client refuses to run
# outside dry-run mode.
# ---------------------------------------------------------------------------
LOGIN_PATH = ""  # e.g. "/api/auth/login"
ORDER_PATH = ""  # e.g. "/api/orders"


class MofidClient(BrokerClient):
    def __init__(self, base_url: str, username: str, password: str, dry_run: bool = True) -> None:
        if not dry_run and not (base_url and LOGIN_PATH and ORDER_PATH):
            raise BrokerError(
                "Mofid connector endpoints are not configured yet. "
                "Fill in LOGIN_PATH/ORDER_PATH in bot/broker/mofid.py from a "
                "captured request, or keep DRY_RUN=true."
            )
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15.0)

    async def login(self) -> None:
        if self.dry_run:
            log.info("[dry-run] skipping real login for user=%s", self.username)
            return

        # TODO: replace with the real login payload/headers once captured.
        resp = await self._client.post(
            LOGIN_PATH,
            json={"username": self.username, "password": self.password},
        )
        if resp.status_code != 200:
            raise BrokerError(f"login failed: HTTP {resp.status_code} {resp.text[:300]}")
        log.info("logged in as %s", self.username)

    async def place_order(self, order: Order) -> str:
        if self.dry_run:
            log.info(
                "[dry-run] would place order: %s %s x%s (%s%s)",
                order.side.value,
                order.symbol,
                order.quantity,
                order.order_type.value,
                f" @ {order.price}" if order.order_type == OrderType.LIMIT else "",
            )
            return f"dry-run-{order.id}"

        # TODO: replace with the real order payload/headers once captured.
        payload = {
            "symbolId": order.symbol_id or order.symbol,
            "side": "buy" if order.side == Side.BUY else "sell",
            "quantity": order.quantity,
            "orderType": order.order_type.value,
            "price": order.price,
        }
        resp = await self._client.post(ORDER_PATH, json=payload)
        if resp.status_code not in (200, 201):
            raise BrokerError(f"order rejected: HTTP {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        order_id = data.get("orderId") or data.get("id")
        if not order_id:
            raise BrokerError(f"order response missing id: {data}")
        return str(order_id)

    async def close(self) -> None:
        await self._client.aclose()
