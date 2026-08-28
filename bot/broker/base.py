from __future__ import annotations

from abc import ABC, abstractmethod

from bot.models import Order


class BrokerError(Exception):
    """Raised when the broker rejects login or an order."""


class OrderRefused(BrokerError):
    """The broker read the order and turned it down in as many words — outside
    trading hours, not enough buying power. Its own message is the message.

    Separate from BrokerError because the scheduler retries a failure, and this
    is not a failure: it is an answer. Retrying it would re-send an order the
    broker has already ruled on, which is pointless when the refusal is real
    and dangerous if it ever is not."""


class BrokerClient(ABC):
    """Interface every broker connector must implement.

    Swap in a different subclass to target a different brokerage without
    touching the scheduler or config loading code.
    """

    @abstractmethod
    async def login(self) -> None:
        ...

    @abstractmethod
    async def place_order(self, order: Order) -> str:
        """Submit the order. Returns the broker's order/ticket id."""

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "BrokerClient":
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
