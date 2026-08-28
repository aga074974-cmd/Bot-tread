from __future__ import annotations

from abc import ABC, abstractmethod

from bot.models import Order


class BrokerError(Exception):
    """Raised when the broker rejects login or an order."""


class FinalBrokerError(BrokerError):
    """A failure there is no point trying again. The scheduler retries an
    ordinary BrokerError, on the chance the next attempt catches a better
    moment; these are settled — the next attempt would meet the same answer."""


class OrderRefused(FinalBrokerError):
    """The broker read the order and turned it down in as many words — outside
    trading hours, not enough buying power. Its own message is the message.

    Final because it is not a failure but an answer. Retrying would re-send an
    order the broker has already ruled on, which is pointless when the refusal
    is real and dangerous if it ever is not."""


class SymbolNotFound(FinalBrokerError):
    """The symbol search came back with nothing, so there is no ticket to open
    and nothing was ordered. Final because a name that does not exist will not
    start existing on the second attempt."""


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
