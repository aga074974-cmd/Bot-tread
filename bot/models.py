from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FILLED = "filled"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: int
    scheduled_at: datetime
    order_type: OrderType = OrderType.MARKET
    price: int | None = None
    symbol_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    ticket_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive: {self.quantity}")
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError(f"limit order for {self.symbol} requires a price")
        if self.scheduled_at.tzinfo is None:
            raise ValueError(f"scheduled_at for {self.symbol} must be timezone-aware")
