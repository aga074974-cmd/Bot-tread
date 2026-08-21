"""The retry loop: how long it keeps trying, and when it decides that a late
order is worse than no order."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.broker.base import BrokerClient, BrokerError
from bot.models import Order, OrderStatus, Side
from bot.scheduler import RETRY_DEADLINE_MARK, run_order


class RecordingBroker(BrokerClient):
    """Counts how many times the scheduler came back for another go."""

    def __init__(self, attempts: list[datetime], fail: bool) -> None:
        self.attempts = attempts
        self.fail = fail

    async def login(self) -> None:
        pass

    async def place_order(self, order: Order) -> str:
        self.attempts.append(datetime.now(timezone.utc))
        if self.fail:
            raise BrokerError("login did not reach the app")
        return f"submitted-{order.id}"


def an_order(*, late_by: float = 0.0) -> Order:
    return Order(
        symbol="دارونو",
        side=Side.BUY,
        quantity=10,
        scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=late_by),
    )


async def run(order: Order, *, fail: bool, max_retries: int, deadline: int) -> list[datetime]:
    attempts: list[datetime] = []
    await run_order(
        order,
        lambda _: RecordingBroker(attempts, fail),
        grace_period_seconds=120,
        max_retries=max_retries,
        retry_delay_seconds=0,
        retry_deadline_seconds=deadline,
    )
    return attempts


async def test_an_order_that_works_is_sent_once():
    order = an_order()

    attempts = await run(order, fail=False, max_retries=5, deadline=90)

    assert len(attempts) == 1
    assert order.status is OrderStatus.SENT


async def test_retrying_stops_at_the_deadline_even_with_attempts_left():
    """Every retry is a fresh browser and a fresh login, so five of them run
    minutes past the scheduled time — by which point this is not the order that
    was asked for any more."""
    order = an_order()

    attempts = await run(order, fail=True, max_retries=5, deadline=0)

    assert len(attempts) == 1  # not 5
    assert order.status is OrderStatus.FAILED
    assert RETRY_DEADLINE_MARK in order.error


async def test_the_cause_survives_next_to_the_deadline_note():
    """Two different facts: the order is not coming, and here is what broke."""
    order = an_order()

    await run(order, fail=True, max_retries=5, deadline=0)

    assert "login did not reach the app" in order.error


async def test_attempts_still_run_out_when_there_is_time_to_spare():
    order = an_order()

    attempts = await run(order, fail=True, max_retries=3, deadline=3600)

    assert len(attempts) == 3
    assert order.status is OrderStatus.FAILED
    assert RETRY_DEADLINE_MARK not in order.error


async def test_an_order_past_its_grace_period_is_never_attempted():
    order = an_order(late_by=600)

    attempts = await run(order, fail=False, max_retries=5, deadline=90)

    assert attempts == []
    assert order.status is OrderStatus.SKIPPED


@pytest.mark.parametrize("deadline,expected", [(0, 1), (3600, 2)])
async def test_the_deadline_is_what_decides_between_them(deadline: int, expected: int):
    order = an_order()

    attempts = await run(order, fail=True, max_retries=2, deadline=deadline)

    assert len(attempts) == expected
