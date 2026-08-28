from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from bot.broker.base import BrokerClient, FinalBrokerError
from bot.models import Order, OrderStatus

log = logging.getLogger(__name__)

BrokerFactory = Callable[[Order], BrokerClient]

# Written into an order's error when retrying was cut short by the deadline
# rather than by running out of attempts. panel/errors.py reads it from here,
# so the two cannot drift apart.
RETRY_DEADLINE_MARK = "stopped: past the retry deadline"
StatusCallback = Callable[[Order], Awaitable[None]] | Callable[[Order], None] | None


async def _sleep_until(target: datetime) -> None:
    """Sleep in short chunks so a system clock change or long sleep drift
    doesn't push us far past the target."""
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 30))


async def _notify(on_status_change: StatusCallback, order: Order) -> None:
    if on_status_change is None:
        return
    result = on_status_change(order)
    if asyncio.iscoroutine(result):
        await result


async def run_order(
    order: Order,
    broker_factory: BrokerFactory,
    grace_period_seconds: int,
    max_retries: int,
    retry_delay_seconds: float,
    retry_deadline_seconds: int,
    on_status_change: StatusCallback = None,
) -> None:
    """Wait until an order's scheduled time, then submit it via its own,
    isolated broker instance (own browser/session) so concurrently-timed
    orders never fight over the same browser page."""
    now = datetime.now(timezone.utc)
    late_by = (now - order.scheduled_at).total_seconds()
    if late_by > grace_period_seconds:
        order.status = OrderStatus.SKIPPED
        log.warning(
            "skipping order %s (%s): %.0fs past scheduled time, exceeds grace period of %ss",
            order.id, order.symbol, late_by, grace_period_seconds,
        )
        await _notify(on_status_change, order)
        return

    await _sleep_until(order.scheduled_at)

    # Each retry costs a browser launch and a login, so a handful of them can
    # run minutes past the scheduled time — by which point the price this order
    # was timed for is gone. Retrying stops at this deadline whether or not the
    # attempts are used up. A single attempt already under way is not cut off:
    # interrupting a half-sent order would be worse than finishing it.
    deadline = order.scheduled_at + timedelta(seconds=retry_deadline_seconds)

    attempt = 0
    while True:
        attempt += 1
        try:
            async with broker_factory(order) as broker:
                ticket_id = await broker.place_order(order)
            order.status = OrderStatus.SENT
            order.ticket_id = ticket_id
            log.info("order %s (%s %s x%s) submitted, ticket=%s", order.id, order.side.value, order.symbol, order.quantity, ticket_id)
            await _notify(on_status_change, order)
            return
        except Exception as exc:
            # Broad on purpose: a Playwright locator timeout, a broker-level
            # BrokerError, or anything else from place_order() must all still
            # retry/fail/notify instead of silently killing this task (which
            # would leave the order stuck at "pending" forever).
            log.error("order %s attempt %s/%s failed: %r", order.id, attempt, max_retries, exc, exc_info=True)

            if isinstance(exc, FinalBrokerError):
                # Settled, so trying again buys nothing: the broker either
                # ruled on the order — and re-sending one it already ruled on
                # is pointless when the refusal is real and dangerous if we
                # ever misread a reply that was not one — or the symbol simply
                # is not there to order.
                order.status = OrderStatus.FAILED
                order.error = str(exc) or repr(exc)
                log.error("order %s (%s) will not be retried: %s", order.id, order.symbol, exc)
                await _notify(on_status_change, order)
                return

            late_by = (datetime.now(timezone.utc) - order.scheduled_at).total_seconds()
            if datetime.now(timezone.utc) >= deadline:
                order.status = OrderStatus.FAILED
                order.error = f"{str(exc) or repr(exc)} — {RETRY_DEADLINE_MARK}"
                log.error(
                    "order %s (%s) abandoned %.0fs past its time (deadline %ss): "
                    "too late to be the order that was asked for",
                    order.id, order.symbol, late_by, retry_deadline_seconds,
                )
                await _notify(on_status_change, order)
                return

            if attempt >= max_retries:
                order.status = OrderStatus.FAILED
                order.error = str(exc) or repr(exc)
                log.error("order %s (%s) exhausted retries, giving up", order.id, order.symbol)
                await _notify(on_status_change, order)
                return

            await asyncio.sleep(retry_delay_seconds)


async def run_all(
    broker_factory: BrokerFactory,
    orders: list[Order],
    grace_period_seconds: int,
    max_retries: int,
    retry_delay_seconds: float,
    retry_deadline_seconds: int,
) -> None:
    if not orders:
        log.warning("no orders loaded, nothing to schedule")
        return

    for order in sorted(orders, key=lambda o: o.scheduled_at):
        log.info("scheduled: %s %s x%s at %s", order.side.value, order.symbol, order.quantity, order.scheduled_at.isoformat())

    tasks = [
        asyncio.create_task(
            run_order(
                order,
                broker_factory,
                grace_period_seconds,
                max_retries,
                retry_delay_seconds,
                retry_deadline_seconds,
            )
        )
        for order in orders
    ]
    await asyncio.gather(*tasks)
