"""SQLAlchemy tables (spec §15). SQLite-first, Postgres-compatible types.

Every decision leaves a row: skipped signals land in `signals` with a
skip_reason, state transitions land in `strategy_events`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MoneyType(TypeDecorator):
    """Exact decimals: NUMERIC on Postgres, TEXT on SQLite (avoids float loss)."""

    impl = Numeric(30, 12)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(48))
        return dialect.type_descriptor(Numeric(30, 12))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(str(value))


MONEY = MoneyType()
UTC_DT = UTCDateTime()


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTC_DT, Decimal: MONEY, dict: JSON, list: JSON}


class TweetRow(Base):
    __tablename__ = "tweets"
    tweet_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    author_id: Mapped[str] = mapped_column(String(30), default="")
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime]
    received_at: Mapped[datetime]
    twitter_latency_ms: Mapped[float]
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_tweets_created", "created_at"),)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(30))
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_trade_signal: Mapped[bool] = mapped_column(Boolean)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    direction: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float]
    signal_stage: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(Text, default="")
    matched_phrases: Mapped[list] = mapped_column(JSON, default=list)
    used_llm: Mapped[bool] = mapped_column(Boolean, default=False)
    classification_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime]
    __table_args__ = (UniqueConstraint("tweet_id", name="uq_signal_tweet"),
                      Index("ix_signals_created", "created_at"))


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(20))
    timestamp: Mapped[datetime]
    label: Mapped[str] = mapped_column(String(30), default="")  # e.g. entry_validation
    reference_price: Mapped[float]
    current_price: Mapped[float]
    price_change_since_tweet_pct: Mapped[float]
    spread_pct: Mapped[float]
    volume_24h_quote: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_liquidity_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_liquidity_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_snapshots_session", "session_id"),)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # intent id
    session_id: Mapped[str] = mapped_column(String(36))
    client_order_id: Mapped[str] = mapped_column(String(40))
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    leg: Mapped[str] = mapped_column(String(6))
    order_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal]
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20))
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_qty: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fee_usdt: Mapped[Decimal] = mapped_column(MONEY, default=0)
    slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_orders_coid"),
                      Index("ix_orders_session", "session_id"))


class TradeRow(Base):
    """One completed round-trip per leg (LONG leg or SHORT leg)."""

    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36))
    tweet_id: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(20))
    leg: Mapped[str] = mapped_column(String(6))
    entry_price: Mapped[float]
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float]
    notional_usdt: Mapped[float]
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None] = mapped_column(UTC_DT, nullable=True)
    close_reason: Mapped[str] = mapped_column(String(60), default="")
    peak_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reversal_score_at_exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_trades_session", "session_id"),
                      Index("ix_trades_closed", "closed_at"))


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(6))
    qty: Mapped[Decimal]
    entry_price: Mapped[Decimal]
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None] = mapped_column(UTC_DT, nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=0)
    close_reason: Mapped[str] = mapped_column(String(60), default="")
    __table_args__ = (Index("ix_positions_open", "symbol", "closed_at"),)


class StrategyEventRow(Base):
    __tablename__ = "strategy_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36))
    timestamp: Mapped[datetime]
    from_state: Mapped[str] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_events_session", "session_id"),)


class DailyStatsRow(Base):
    __tablename__ = "daily_stats"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime]
