from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.config import TEHRAN_TZ
from bot.models import Order, OrderStatus, OrderType, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    symbol_id TEXT,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    price INTEGER,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    ticket_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


class OrderStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(SCHEMA)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert(self, order: Order) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT INTO orders
               (id, symbol, symbol_id, side, quantity, order_type, price, scheduled_at, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.id,
                order.symbol,
                order.symbol_id,
                order.side.value,
                order.quantity,
                order.order_type.value,
                order.price,
                order.scheduled_at.isoformat(),
                order.status.value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    async def insert(self, order: Order) -> None:
        await asyncio.to_thread(self._insert, order)

    def _update_status(self, order: Order, error: str | None) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE orders SET status = ?, ticket_id = ?, error = ? WHERE id = ?",
            (order.status.value, order.ticket_id, error, order.id),
        )
        conn.commit()
        conn.close()

    async def update_status(self, order: Order, error: str | None = None) -> None:
        await asyncio.to_thread(self._update_status, order, error)

    def _list(self) -> list[sqlite3.Row]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM orders ORDER BY scheduled_at DESC").fetchall()
        conn.close()
        return rows

    async def list_all(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list)

    def _pending(self) -> list[Order]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM orders WHERE status = 'pending'").fetchall()
        conn.close()
        return [row_to_order(r) for r in rows]

    async def pending(self) -> list[Order]:
        return await asyncio.to_thread(self._pending)

    def _cancel(self, order_id: str) -> bool:
        conn = self._connect()
        cur = conn.execute(
            "UPDATE orders SET status = 'skipped' WHERE id = ? AND status = 'pending'",
            (order_id,),
        )
        conn.commit()
        changed = cur.rowcount > 0
        conn.close()
        return changed

    async def cancel(self, order_id: str) -> bool:
        return await asyncio.to_thread(self._cancel, order_id)

    def _purge_old(self, retention_days: int) -> int:
        conn = self._connect()
        # scheduled_at is stored with whatever UTC offset TEHRAN_TZ carried at
        # insert time; comparing the parsed instants (not the raw strings)
        # keeps this correct even if that offset were ever to change.
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        rows = conn.execute("SELECT id, scheduled_at FROM orders").fetchall()
        stale_ids = [r["id"] for r in rows if datetime.fromisoformat(r["scheduled_at"]) < cutoff]
        if stale_ids:
            conn.executemany("DELETE FROM orders WHERE id = ?", [(i,) for i in stale_ids])
            conn.commit()
        conn.close()
        return len(stale_ids)

    async def purge_old(self, retention_days: int) -> int:
        """Drop every order — pending included — whose scheduled_at is more
        than retention_days in the past. A pending order this old is already
        long past any grace period; keeping it around serves no one."""
        return await asyncio.to_thread(self._purge_old, retention_days)


def row_to_order(row: sqlite3.Row) -> Order:
    scheduled_at = datetime.fromisoformat(row["scheduled_at"])
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=TEHRAN_TZ)
    order = Order(
        symbol=row["symbol"],
        symbol_id=row["symbol_id"],
        side=Side(row["side"]),
        quantity=row["quantity"],
        order_type=OrderType(row["order_type"]),
        price=row["price"],
        scheduled_at=scheduled_at,
        id=row["id"],
    )
    order.status = OrderStatus(row["status"])
    return order
