"""SQLAlchemy 2 table definitions.

Types are kept portable between PostgreSQL (production) and SQLite (tests):
JSON, DateTime(timezone=True), Numeric for money/quantities, Float for analytics.
Retention policies for high-frequency tables: see app/storage/retention.py.
"""

from __future__ import annotations

from datetime import datetime
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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(38, 18)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        Decimal: MONEY,
        dict: JSON,
        list: JSON,
    }


# ── market data ──────────────────────────────────────────────────────────────


class MarketCandleRow(Base):
    __tablename__ = "market_candles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(4))
    open_time: Mapped[datetime]
    close_time: Mapped[datetime]
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume: Mapped[float]
    quote_volume: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    taker_buy_base: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),
        Index("ix_candles_lookup", "symbol", "timeframe", "open_time"),
    )


class MarketEventRow(Base):
    """Raw market events (replayable). High-frequency — retention-managed."""

    __tablename__ = "market_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timestamp_event: Mapped[datetime]
    timestamp_received: Mapped[datetime]
    event_hash: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict]
    __table_args__ = (
        UniqueConstraint("event_hash", name="uq_market_event_hash"),
        Index("ix_market_events_time", "timestamp_event"),
        Index("ix_market_events_symbol", "symbol", "kind", "timestamp_event"),
    )


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    trade_id: Mapped[int] = mapped_column(Integer)
    price: Mapped[float]
    qty: Mapped[float]
    is_buyer_maker: Mapped[bool] = mapped_column(Boolean)
    timestamp: Mapped[datetime]
    __table_args__ = (
        UniqueConstraint("symbol", "trade_id", name="uq_trade"),
        Index("ix_trades_symbol_time", "symbol", "timestamp"),
    )


class OrderbookSnapshotRow(Base):
    __tablename__ = "orderbook_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    timestamp: Mapped[datetime]
    bids: Mapped[list]
    asks: Mapped[list]
    __table_args__ = (Index("ix_ob_symbol_time", "symbol", "timestamp"),)


# ── external intelligence ────────────────────────────────────────────────────


class ExternalEventRow(Base):
    __tablename__ = "external_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assets: Mapped[list]
    category: Mapped[str] = mapped_column(String(30))
    headline: Mapped[str] = mapped_column(String(500))
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    timestamp: Mapped[datetime]
    source: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(20))
    reliability: Mapped[float]
    sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    novelty: Mapped[float] = mapped_column(Float, default=1.0)
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    decay_half_life_hours: Mapped[float] = mapped_column(Float, default=12.0)
    event_hash: Mapped[str] = mapped_column(String(64))
    cluster_key: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("event_hash", name="uq_external_event_hash"),
        Index("ix_ext_events_cluster", "cluster_key"),
        Index("ix_ext_events_time", "timestamp"),
    )


class SourceRow(Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(50), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.5)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceHealthRow(Base):
    __tablename__ = "source_health"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100))
    timestamp: Mapped[datetime]
    healthy: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(String(500), default="")
    events_seen: Mapped[int] = mapped_column(Integer, default=0)
    lag_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_source_health", "source", "timestamp"),)


# ── features / signals / decisions ───────────────────────────────────────────


class FeatureRow(Base):
    __tablename__ = "features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(4))
    timestamp: Mapped[datetime]
    values: Mapped[dict]
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_feature"),
        Index("ix_features_lookup", "symbol", "timeframe", "timestamp"),
    )


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(60))
    strategy_version: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(6))
    timeframe: Mapped[str] = mapped_column(String(4))
    timestamp: Mapped[datetime]
    reference_price: Mapped[float]
    confidence: Mapped[float]
    expected_edge_bps: Mapped[float] = mapped_column(Float, default=0.0)
    invalidation_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    hypothetical_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    hypothetical_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_regime: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    data_quality: Mapped[float] = mapped_column(Float, default=1.0)
    external_confirmation: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict]  # full signal incl. evidence & features_used
    __table_args__ = (Index("ix_signals_symbol_time", "symbol", "timestamp"),)


class SignalEvidenceRow(Base):
    __tablename__ = "signal_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(String(200))
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    supports: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (Index("ix_evidence_signal", "signal_id"),)


class DecisionRow(Base):
    __tablename__ = "decision_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(60))
    timestamp: Mapped[datetime]
    decision: Mapped[str] = mapped_column(String(12))
    failed_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(12), nullable=True)
    stages: Mapped[list]
    explanation: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (Index("ix_decisions_time", "timestamp"),)


# ── strategies / backtests ───────────────────────────────────────────────────


class StrategyRow(Base):
    __tablename__ = "strategies"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    stage: Mapped[str] = mapped_column(String(15), default="EXPERIMENTAL")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version: Mapped[str] = mapped_column(String(20), default="1.0")
    scorecard: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime]


class StrategyVersionRow(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(60))
    version: Mapped[str] = mapped_column(String(20))
    params: Mapped[dict]
    created_at: Mapped[datetime]
    note: Mapped[str] = mapped_column(String(500), default="")
    __table_args__ = (UniqueConstraint("strategy", "version", name="uq_strategy_version"),)


class BacktestRow(Base):
    __tablename__ = "backtests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(60))
    strategy_version: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(4))
    start: Mapped[datetime]
    end: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(20), default="in_sample")  # in_sample|oos|walk_forward
    params: Mapped[dict]
    metrics: Mapped[dict]
    created_at: Mapped[datetime]
    __table_args__ = (Index("ix_backtests_strategy", "strategy", "created_at"),)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(6))
    entry_time: Mapped[datetime]
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float]
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float]
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(50), default="")
    __table_args__ = (Index("ix_bt_trades", "backtest_id"),)


# ── orders / fills / positions (paper + testnet share shape via venue) ───────


class TradeIntentRow(Base):
    __tablename__ = "trade_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(36))
    decision_id: Mapped[str] = mapped_column(String(36), default="")
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(6))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(10))
    signal_score: Mapped[float] = mapped_column(Float, default=0.0)
    reference_price: Mapped[Decimal]
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    quantity: Mapped[Decimal]
    hypothetical_stop: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    hypothetical_target: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[str] = mapped_column(String(500), default="")
    venue: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime]


class _OrderColumns:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(36))
    client_order_id: Mapped[str] = mapped_column(String(40))
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(10))
    time_in_force: Mapped[str] = mapped_column(String(3), default="GTC")
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    filled_qty: Mapped[Decimal] = mapped_column(MONEY, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(String(500), default="")


class PaperOrderRow(_OrderColumns, Base):
    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_paper_orders_coid"),
        Index("ix_paper_orders_symbol", "symbol", "status"),
    )


class TestnetOrderRow(_OrderColumns, Base):
    __tablename__ = "testnet_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_testnet_orders_coid"),
        Index("ix_testnet_orders_symbol", "symbol", "status"),
    )


class _FillColumns:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[Decimal] = mapped_column(MONEY)
    qty: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fee_asset: Mapped[str] = mapped_column(String(10), default="USDT")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_maker: Mapped[bool] = mapped_column(Boolean, default=False)


class PaperFillRow(_FillColumns, Base):
    __tablename__ = "paper_fills"
    __table_args__ = (Index("ix_paper_fills_order", "order_id"),)


class TestnetFillRow(_FillColumns, Base):
    __tablename__ = "testnet_fills"
    __table_args__ = (Index("ix_testnet_fills_order", "order_id"),)


class _PositionColumns:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(6), default="LONG")
    qty: Mapped[Decimal] = mapped_column(MONEY)
    avg_entry_price: Mapped[Decimal] = mapped_column(MONEY)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy: Mapped[str] = mapped_column(String(60), default="")
    signal_id: Mapped[str] = mapped_column(String(36), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, default=0)
    close_reason: Mapped[str] = mapped_column(String(100), default="")


class PaperPositionRow(_PositionColumns, Base):
    __tablename__ = "paper_positions"
    __table_args__ = (Index("ix_paper_positions_open", "symbol", "closed_at"),)


class TestnetPositionRow(_PositionColumns, Base):
    __tablename__ = "testnet_positions"
    __table_args__ = (Index("ix_testnet_positions_open", "symbol", "closed_at"),)


# ── risk / regimes / system ──────────────────────────────────────────────────


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[str] = mapped_column(String(1000), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_risk_events_time", "timestamp"),)


class RegimeHistoryRow(Base):
    __tablename__ = "regime_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    timeframe: Mapped[str] = mapped_column(String(4))
    timestamp: Mapped[datetime]
    regime: Mapped[str] = mapped_column(String(20))
    volatility_state: Mapped[str] = mapped_column(String(20), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_regime_hist", "symbol", "timestamp"),)


class SystemEventRow(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(1000), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_system_events_time", "timestamp"),)


class PerformanceSnapshotRow(Base):
    __tablename__ = "performance_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime]
    venue: Mapped[str] = mapped_column(String(12))
    equity: Mapped[Decimal]
    cash: Mapped[Decimal]
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    realized_pnl_today: Mapped[Decimal] = mapped_column(MONEY, default=0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_perf_snap", "venue", "timestamp"),)
