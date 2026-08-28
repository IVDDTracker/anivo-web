"""Domain models shared by live, paper and backtest paths."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TweetKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    REPLY = "REPLY"
    QUOTE = "QUOTE"
    RETWEET = "RETWEET"


class TweetEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tweet_id: str
    author_id: str = ""
    text: str
    kind: TweetKind = TweetKind.ORIGINAL
    created_at: datetime
    received_at: datetime
    raw: dict = Field(default_factory=dict)

    @property
    def latency_ms(self) -> float:
        return max(0.0, (self.received_at - self.created_at).total_seconds() * 1000.0)

    def age_seconds(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds()


class SignalStage(StrEnum):
    EARLY = "EARLY"
    CONFIRMED = "CONFIRMED"
    NONE = "NONE"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"
    COMMENT = "COMMENT"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    UNKNOWN = "UNKNOWN"


class Classification(BaseModel):
    """Exactly the structure required by spec §3 (+ diagnostics)."""

    model_config = ConfigDict(extra="ignore")

    is_trade_signal: bool
    symbol: str | None = None
    direction: Direction = Direction.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    signal_stage: SignalStage = SignalStage.NONE
    action: SignalAction = SignalAction.COMMENT
    reason: str = ""
    tweet_id: str = ""
    matched_phrases: list[str] = Field(default_factory=list)
    used_llm: bool = False
    classification_latency_ms: float = 0.0


class SkipReason(StrEnum):
    TWEET_TOO_OLD = "TWEET_TOO_OLD"
    NOT_TRADE_SIGNAL = "NOT_TRADE_SIGNAL"
    SYMBOL_NOT_ON_BINANCE = "SYMBOL_NOT_ON_BINANCE"
    PRICE_ALREADY_PUMPED = "PRICE_ALREADY_PUMPED"
    SPREAD_TOO_HIGH = "SPREAD_TOO_HIGH"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    RISK_LIMIT = "RISK_LIMIT"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    KILL_SWITCH = "KILL_SWITCH"
    DUPLICATE = "DUPLICATE"
    DATA_STALE = "DATA_STALE"
    EARLY_SIGNAL_DISABLED = "EARLY_SIGNAL_DISABLED"
    LATENCY_TOO_HIGH = "LATENCY_TOO_HIGH"


class AggTrade(BaseModel):
    model_config = ConfigDict(extra="ignore")

    price: float
    qty: float
    timestamp: datetime
    is_buyer_maker: bool  # True → aggressive SELL

    @property
    def quote_qty(self) -> float:
        return self.price * self.qty


class BookTop(BaseModel):
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_pct(self) -> float:
        return (self.ask_price - self.bid_price) / self.mid * 100.0 if self.mid > 0 else 0.0


class MarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    reference_price: float
    current_price: float
    price_change_since_tweet_pct: float
    spread_pct: float
    volume_24h_quote: float | None = None
    bid_liquidity_usdt: float | None = None
    ask_liquidity_usdt: float | None = None


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderIntent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    order_type: str = "MARKET"
    limit_price: Decimal | None = None
    reduce_only: bool = False
    leg: str = "LONG"  # LONG | SHORT (which leg of the session)
    reason: str = ""
    created_at: datetime


class OrderResult(BaseModel):
    intent_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    status: OrderStatus
    requested_price: float | None = None
    executed_price: float | None = None
    executed_qty: Decimal = Decimal("0")
    fee_usdt: Decimal = Decimal("0")
    slippage_pct: float | None = None
    order_latency_ms: float = 0.0
    error: str = ""


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    symbol: str
    side: PositionSide
    qty: Decimal
    entry_price: Decimal
    opened_at: datetime
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    close_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.closed_at is None
