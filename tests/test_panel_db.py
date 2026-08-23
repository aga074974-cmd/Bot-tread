"""OrderStore.purge_old — dropping orders once they are too old to matter,
regardless of how they ended up (pending included: one that old is already
long past any grace period)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.config import TEHRAN_TZ
from bot.models import Order, OrderStatus, Side
from panel.db import OrderStore


@pytest.fixture
def store(tmp_path) -> OrderStore:
    return OrderStore(str(tmp_path / "panel.db"))


def an_order(*, days_old: int) -> Order:
    return Order(
        symbol="دارونو",
        side=Side.BUY,
        quantity=10,
        scheduled_at=datetime.now(TEHRAN_TZ) - timedelta(days=days_old),
    )


async def test_orders_within_the_window_survive(store: OrderStore):
    order = an_order(days_old=10)
    await store.insert(order)

    removed = await store.purge_old(retention_days=30)

    assert removed == 0
    assert len(await store.list_all()) == 1


async def test_orders_past_the_window_are_deleted(store: OrderStore):
    order = an_order(days_old=45)
    await store.insert(order)

    removed = await store.purge_old(retention_days=30)

    assert removed == 1
    assert await store.list_all() == []


async def test_a_pending_order_is_deleted_too_once_old_enough(store: OrderStore):
    """A pending order this old already missed its grace period long ago;
    nothing is served by keeping it around forever."""
    order = an_order(days_old=45)
    order.status = OrderStatus.PENDING
    await store.insert(order)

    removed = await store.purge_old(retention_days=30)

    assert removed == 1


async def test_only_the_stale_ones_go(store: OrderStore):
    fresh = an_order(days_old=1)
    stale = an_order(days_old=60)
    await store.insert(fresh)
    await store.insert(stale)

    removed = await store.purge_old(retention_days=30)

    assert removed == 1
    remaining = await store.list_all()
    assert len(remaining) == 1
    assert remaining[0]["id"] == fresh.id


async def test_just_inside_the_window_is_kept(store: OrderStore):
    """29 days old, 30-day retention: still inside the window."""
    order = an_order(days_old=29)
    await store.insert(order)

    removed = await store.purge_old(retention_days=30)

    assert removed == 0
