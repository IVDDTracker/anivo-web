"""Order/position/intent domain models. Decimal at every exchange boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Direction, OrderSide, OrderStatus, OrderType, TimeInForce, Venue


class TradeIntent(BaseModel):
    """A hypothetical order the pipeline produced. Persisted BEFORE any submission."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str
    decision_id: str = ""
    symbol: str
    direction: Direction
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    signal_score: float = 0.0
    reference_price: Decimal
    limit_price: Decimal | None = None
    quantity: Decimal
    hypothetical_stop: Decimal | None = None
    hypothetical_target: Decimal | None = None
    reason: str = ""
    invalidation: str = ""
    venue: Venue = Venue.PAPER
    strategy: str = ""
    created_at: datetime


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    venue: Venue
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce = TimeInForce.GTC
    quantity: Decimal
    price: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    strategy: str = ""
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    error: str = ""


class Fill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    venue: Venue
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    fee: Decimal = Decimal("0")
    fee_asset: str = "USDT"
    timestamp: datetime
    is_maker: bool = False


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    venue: Venue
    symbol: str
    direction: Direction = Direction.LONG
    qty: Decimal = Decimal("0")
    avg_entry_price: Decimal = Decimal("0")
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    strategy: str = ""
    signal_id: str = ""
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    close_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed_at is None and self.qty > 0

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        if not self.is_open:
            return Decimal("0")
        sign = Decimal("1") if self.direction == Direction.LONG else Decimal("-1")
        return sign * (mark_price - self.avg_entry_price) * self.qty


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    original_quantity: Decimal | None = None
    approved_quantity: Decimal | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
