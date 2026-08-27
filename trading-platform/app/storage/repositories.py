"""Repositories: the only place SQL lives. Domain models in, domain models out."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger
from app.models.enums import Venue
from app.models.events import ExternalEvent, RawEvent
from app.models.market import Candle
from app.models.orders import Fill, Order, Position, TradeIntent
from app.models.signals import DecisionRecord, Signal
from app.storage.db import Database
from app.storage.tables import (
    BacktestRow,
    BacktestTradeRow,
    DecisionRow,
    ExternalEventRow,
    FeatureRow,
    MarketCandleRow,
    MarketEventRow,
    OrderbookSnapshotRow,
    PaperFillRow,
    PaperOrderRow,
    PaperPositionRow,
    PerformanceSnapshotRow,
    RegimeHistoryRow,
    RiskEventRow,
    SignalRow,
    SourceHealthRow,
    SystemEventRow,
    TestnetFillRow,
    TestnetOrderRow,
    TestnetPositionRow,
    TradeIntentRow,
    TradeRow,
)

log = get_logger(__name__)


def _order_tables(venue: Venue) -> tuple[type, type, type]:
    if venue == Venue.PAPER:
        return PaperOrderRow, PaperFillRow, PaperPositionRow
    if venue == Venue.TESTNET:
        return TestnetOrderRow, TestnetFillRow, TestnetPositionRow
    raise ValueError(f"no order tables for venue {venue}")  # PRODUCTION: intents only, by design


class CandleRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, candle: Candle) -> None:
        async with self.db.session() as s:
            existing = await s.scalar(
                select(MarketCandleRow).where(
                    MarketCandleRow.symbol == candle.symbol,
                    MarketCandleRow.timeframe == candle.timeframe,
                    MarketCandleRow.open_time == candle.open_time,
                )
            )
            if existing:
                for field in ("open", "high", "low", "close", "volume", "quote_volume", "trades",
                              "taker_buy_base"):
                    setattr(existing, field, getattr(candle, field))
                existing.close_time = candle.close_time
            else:
                s.add(MarketCandleRow(**candle.model_dump(exclude={"closed"})))

    async def insert_many(self, candles: list[Candle]) -> int:
        inserted = 0
        for candle in candles:
            try:
                async with self.db.session() as s:
                    s.add(MarketCandleRow(**candle.model_dump(exclude={"closed"})))
                inserted += 1
            except IntegrityError:
                pass  # already stored
        return inserted

    async def fetch(
        self, symbol: str, timeframe: str, *, start: datetime | None = None,
        end: datetime | None = None, limit: int | None = None,
    ) -> list[Candle]:
        stmt = select(MarketCandleRow).where(
            MarketCandleRow.symbol == symbol, MarketCandleRow.timeframe == timeframe
        )
        if start:
            stmt = stmt.where(MarketCandleRow.open_time >= start)
        if end:
            stmt = stmt.where(MarketCandleRow.open_time <= end)
        stmt = stmt.order_by(MarketCandleRow.open_time.desc() if limit else MarketCandleRow.open_time)
        if limit:
            stmt = stmt.limit(limit)
        async with self.db.session() as s:
            rows = (await s.scalars(stmt)).all()
        candles = [
            Candle(
                symbol=r.symbol, timeframe=r.timeframe, open_time=r.open_time, close_time=r.close_time,
                open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                quote_volume=r.quote_volume, trades=r.trades, taker_buy_base=r.taker_buy_base,
            )
            for r in rows
        ]
        if limit:
            candles.reverse()
        return candles

    async def latest_open_time(self, symbol: str, timeframe: str) -> datetime | None:
        async with self.db.session() as s:
            return await s.scalar(
                select(func.max(MarketCandleRow.open_time)).where(
                    MarketCandleRow.symbol == symbol, MarketCandleRow.timeframe == timeframe
                )
            )


class EventRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def store_raw(self, event: RawEvent) -> bool:
        """Persist a raw market event; returns False if it was a duplicate."""
        try:
            async with self.db.session() as s:
                s.add(
                    MarketEventRow(
                        event_id=event.id, source=event.source, kind=event.kind, symbol=event.symbol,
                        timestamp_event=event.timestamp_event,
                        timestamp_received=event.timestamp_received,
                        event_hash=event.event_hash, raw_payload=event.raw_payload,
                    )
                )
            return True
        except IntegrityError:
            return False

    async def store_external(self, event: ExternalEvent) -> bool:
        try:
            async with self.db.session() as s:
                s.add(ExternalEventRow(**event.model_dump()))
            return True
        except IntegrityError:
            return False

    async def update_external_confirmation(
        self, event_id: str, *, confirmation_count: int, confidence: float, novelty: float
    ) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(ExternalEventRow)
                .where(ExternalEventRow.id == event_id)
                .values(confirmation_count=confirmation_count, confidence=confidence, novelty=novelty)
            )

    async def cluster_events(self, cluster_key: str, since: datetime) -> list[ExternalEvent]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(ExternalEventRow).where(
                        ExternalEventRow.cluster_key == cluster_key,
                        ExternalEventRow.timestamp >= since,
                    )
                )
            ).all()
        return [ExternalEvent.model_validate(r, from_attributes=True) for r in rows]

    async def recent_external(
        self, *, since: datetime, asset: str | None = None, limit: int = 200
    ) -> list[ExternalEvent]:
        stmt = (
            select(ExternalEventRow)
            .where(ExternalEventRow.timestamp >= since)
            .order_by(ExternalEventRow.timestamp.desc())
            .limit(limit)
        )
        async with self.db.session() as s:
            rows = (await s.scalars(stmt)).all()
        events = [ExternalEvent.model_validate(r, from_attributes=True) for r in rows]
        if asset:
            events = [e for e in events if asset in e.assets]
        return events

    async def raw_events_between(
        self, start: datetime, end: datetime, *, kinds: list[str] | None = None
    ) -> list[RawEvent]:
        stmt = (
            select(MarketEventRow)
            .where(MarketEventRow.timestamp_event >= start, MarketEventRow.timestamp_event < end)
            .order_by(MarketEventRow.timestamp_event)
        )
        if kinds:
            stmt = stmt.where(MarketEventRow.kind.in_(kinds))
        async with self.db.session() as s:
            rows = (await s.scalars(stmt)).all()
        return [
            RawEvent(
                id=r.event_id, source=r.source, source_type="MARKET", kind=r.kind, symbol=r.symbol,
                timestamp_event=r.timestamp_event, timestamp_received=r.timestamp_received,
                event_hash=r.event_hash, raw_payload=r.raw_payload,
            )
            for r in rows
        ]


class SignalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def store_signal(self, signal: Signal) -> None:
        async with self.db.session() as s:
            s.add(
                SignalRow(
                    id=signal.id, symbol=signal.symbol, strategy=signal.strategy,
                    strategy_version=signal.strategy_version, direction=signal.direction.value,
                    timeframe=signal.timeframe, timestamp=signal.timestamp,
                    reference_price=signal.reference_price, confidence=signal.confidence,
                    expected_edge_bps=signal.expected_edge_bps,
                    invalidation_level=signal.invalidation_level,
                    hypothetical_stop=signal.hypothetical_stop,
                    hypothetical_target=signal.hypothetical_target,
                    market_regime=signal.market_regime.value, data_quality=signal.data_quality,
                    external_confirmation=signal.external_confirmation,
                    payload=signal.model_dump(mode="json"),
                )
            )

    async def store_decision(self, record: DecisionRecord) -> None:
        async with self.db.session() as s:
            s.add(
                DecisionRow(
                    id=record.id, signal_id=record.signal_id, symbol=record.symbol,
                    strategy=record.strategy, timestamp=record.timestamp,
                    decision=record.decision.value,
                    failed_stage=(fs.value if (fs := record.failed_stage()) else None),
                    venue=record.venue.value if record.venue else None,
                    stages=[st.model_dump(mode="json") for st in record.stages],
                    explanation=record.explanation,
                )
            )

    async def recent_signals(self, *, limit: int = 50, since: datetime | None = None) -> list[dict]:
        stmt = select(SignalRow).order_by(SignalRow.timestamp.desc()).limit(limit)
        if since:
            stmt = stmt.where(SignalRow.timestamp >= since)
        async with self.db.session() as s:
            rows = (await s.scalars(stmt)).all()
        return [r.payload for r in rows]

    async def recent_decisions(self, *, limit: int = 50, since: datetime | None = None) -> list[dict]:
        stmt = select(DecisionRow).order_by(DecisionRow.timestamp.desc()).limit(limit)
        if since:
            stmt = stmt.where(DecisionRow.timestamp >= since)
        async with self.db.session() as s:
            rows = (await s.scalars(stmt)).all()
        return [
            {
                "id": r.id, "signal_id": r.signal_id, "symbol": r.symbol, "strategy": r.strategy,
                "timestamp": r.timestamp.isoformat(), "decision": r.decision,
                "failed_stage": r.failed_stage, "venue": r.venue, "stages": r.stages,
                "explanation": r.explanation,
            }
            for r in rows
        ]


class OrderRepository:
    def __init__(self, db: Database, venue: Venue) -> None:
        self.db = db
        self.venue = venue
        self._order_t, self._fill_t, self._pos_t = _order_tables(venue)

    async def store_intent(self, intent: TradeIntent) -> None:
        async with self.db.session() as s:
            data = intent.model_dump(exclude={"time_in_force"})
            data["direction"] = intent.direction.value
            data["side"] = intent.side.value
            data["order_type"] = intent.order_type.value
            data["venue"] = intent.venue.value
            s.add(TradeIntentRow(**data))

    async def store_order(self, order: Order) -> None:
        async with self.db.session() as s:
            s.add(
                self._order_t(
                    id=order.id, intent_id=order.intent_id, client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id, symbol=order.symbol,
                    side=order.side.value, order_type=order.order_type.value,
                    time_in_force=order.time_in_force.value, quantity=order.quantity,
                    price=order.price, status=order.status.value, filled_qty=order.filled_qty,
                    avg_fill_price=order.avg_fill_price, strategy=order.strategy,
                    created_at=order.created_at, updated_at=order.updated_at, error=order.error,
                )
            )

    async def update_order(self, order: Order) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(self._order_t)
                .where(self._order_t.id == order.id)
                .values(
                    status=order.status.value, filled_qty=order.filled_qty,
                    avg_fill_price=order.avg_fill_price, exchange_order_id=order.exchange_order_id,
                    updated_at=order.updated_at, error=order.error,
                )
            )

    async def get_order(self, order_id: str) -> Order | None:
        async with self.db.session() as s:
            r = await s.get(self._order_t, order_id)
        return self._to_order(r) if r else None

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        async with self.db.session() as s:
            r = await s.scalar(select(self._order_t).where(self._order_t.client_order_id == client_order_id))
        return self._to_order(r) if r else None

    async def orders_with_status(self, statuses: list[str]) -> list[Order]:
        async with self.db.session() as s:
            rows = (await s.scalars(select(self._order_t).where(self._order_t.status.in_(statuses)))).all()
        return [self._to_order(r) for r in rows]

    def _to_order(self, r) -> Order:
        return Order(
            id=r.id, intent_id=r.intent_id, client_order_id=r.client_order_id,
            exchange_order_id=r.exchange_order_id, venue=self.venue, symbol=r.symbol, side=r.side,
            order_type=r.order_type, time_in_force=r.time_in_force, quantity=r.quantity,
            price=r.price, status=r.status, filled_qty=r.filled_qty, avg_fill_price=r.avg_fill_price,
            strategy=r.strategy, created_at=r.created_at, updated_at=r.updated_at, error=r.error,
        )

    async def store_fill(self, fill: Fill) -> None:
        async with self.db.session() as s:
            s.add(
                self._fill_t(
                    id=fill.id, order_id=fill.order_id, symbol=fill.symbol, side=fill.side.value,
                    price=fill.price, qty=fill.qty, fee=fill.fee, fee_asset=fill.fee_asset,
                    timestamp=fill.timestamp, is_maker=fill.is_maker,
                )
            )

    async def fills_between(self, start: datetime, end: datetime) -> list[Fill]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(self._fill_t).where(
                        self._fill_t.timestamp >= start, self._fill_t.timestamp < end
                    )
                )
            ).all()
        return [
            Fill(
                id=r.id, order_id=r.order_id, venue=self.venue, symbol=r.symbol, side=r.side,
                price=r.price, qty=r.qty, fee=r.fee, fee_asset=r.fee_asset, timestamp=r.timestamp,
                is_maker=r.is_maker,
            )
            for r in rows
        ]

    async def store_position(self, pos: Position) -> None:
        async with self.db.session() as s:
            data = pos.model_dump(exclude={"venue"})
            data["direction"] = pos.direction.value
            s.add(self._pos_t(**data))

    async def update_position(self, pos: Position) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(self._pos_t)
                .where(self._pos_t.id == pos.id)
                .values(
                    qty=pos.qty, avg_entry_price=pos.avg_entry_price, stop_price=pos.stop_price,
                    target_price=pos.target_price, closed_at=pos.closed_at,
                    realized_pnl=pos.realized_pnl, fees_paid=pos.fees_paid,
                    close_reason=pos.close_reason,
                )
            )

    async def open_positions(self) -> list[Position]:
        async with self.db.session() as s:
            rows = (await s.scalars(select(self._pos_t).where(self._pos_t.closed_at.is_(None)))).all()
        return [self._to_position(r) for r in rows]

    async def positions_closed_between(self, start: datetime, end: datetime) -> list[Position]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(self._pos_t).where(
                        self._pos_t.closed_at >= start, self._pos_t.closed_at < end
                    )
                )
            ).all()
        return [self._to_position(r) for r in rows]

    def _to_position(self, r) -> Position:
        return Position(
            id=r.id, venue=self.venue, symbol=r.symbol, direction=r.direction, qty=r.qty,
            avg_entry_price=r.avg_entry_price, stop_price=r.stop_price, target_price=r.target_price,
            strategy=r.strategy, signal_id=r.signal_id, opened_at=r.opened_at, closed_at=r.closed_at,
            realized_pnl=r.realized_pnl, fees_paid=r.fees_paid, close_reason=r.close_reason,
        )


class SystemRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def risk_event(self, ts: datetime, kind: str, detail: str, *, symbol: str | None = None,
                         payload: dict | None = None) -> None:
        async with self.db.session() as s:
            s.add(RiskEventRow(timestamp=ts, kind=kind, symbol=symbol, detail=detail,
                               payload=payload or {}))

    async def system_event(self, ts: datetime, kind: str, detail: str, payload: dict | None = None) -> None:
        async with self.db.session() as s:
            s.add(SystemEventRow(timestamp=ts, kind=kind, detail=detail, payload=payload or {}))

    async def regime_change(self, ts: datetime, symbol: str, timeframe: str, regime: str,
                            volatility_state: str, detail: dict) -> None:
        async with self.db.session() as s:
            s.add(RegimeHistoryRow(symbol=symbol, timeframe=timeframe, timestamp=ts, regime=regime,
                                   volatility_state=volatility_state, detail=detail))

    async def latest_regime(self, symbol: str, timeframe: str) -> dict | None:
        async with self.db.session() as s:
            r = await s.scalar(
                select(RegimeHistoryRow)
                .where(RegimeHistoryRow.symbol == symbol, RegimeHistoryRow.timeframe == timeframe)
                .order_by(RegimeHistoryRow.timestamp.desc())
                .limit(1)
            )
        if not r:
            return None
        return {"symbol": r.symbol, "regime": r.regime, "volatility_state": r.volatility_state,
                "timestamp": r.timestamp.isoformat(), "detail": r.detail}

    async def source_health(self, ts: datetime, source: str, healthy: bool, detail: str,
                            events_seen: int, lag_seconds: float) -> None:
        async with self.db.session() as s:
            s.add(SourceHealthRow(source=source, timestamp=ts, healthy=healthy, detail=detail,
                                  events_seen=events_seen, lag_seconds=lag_seconds))

    async def latest_source_health(self) -> list[dict]:
        async with self.db.session() as s:
            sub = (
                select(SourceHealthRow.source, func.max(SourceHealthRow.timestamp).label("ts"))
                .group_by(SourceHealthRow.source)
                .subquery()
            )
            rows = (
                await s.scalars(
                    select(SourceHealthRow).join(
                        sub,
                        (SourceHealthRow.source == sub.c.source)
                        & (SourceHealthRow.timestamp == sub.c.ts),
                    )
                )
            ).all()
        return [
            {"source": r.source, "healthy": r.healthy, "detail": r.detail,
             "events_seen": r.events_seen, "lag_seconds": r.lag_seconds,
             "timestamp": r.timestamp.isoformat()}
            for r in rows
        ]

    async def performance_snapshot(self, ts: datetime, venue: str, equity, cash, unrealized,
                                   realized_today, drawdown_pct: float, open_positions: int,
                                   detail: dict | None = None) -> None:
        async with self.db.session() as s:
            s.add(
                PerformanceSnapshotRow(
                    timestamp=ts, venue=venue, equity=equity, cash=cash, unrealized_pnl=unrealized,
                    realized_pnl_today=realized_today, drawdown_pct=drawdown_pct,
                    open_positions=open_positions, detail=detail or {},
                )
            )

    async def equity_curve(self, venue: str, since: datetime) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(PerformanceSnapshotRow)
                    .where(PerformanceSnapshotRow.venue == venue,
                           PerformanceSnapshotRow.timestamp >= since)
                    .order_by(PerformanceSnapshotRow.timestamp)
                )
            ).all()
        return [
            {"timestamp": r.timestamp.isoformat(), "equity": float(r.equity),
             "drawdown_pct": r.drawdown_pct}
            for r in rows
        ]

    async def recent_risk_events(self, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(RiskEventRow).order_by(RiskEventRow.timestamp.desc()).limit(limit)
                )
            ).all()
        return [
            {"timestamp": r.timestamp.isoformat(), "kind": r.kind, "symbol": r.symbol,
             "detail": r.detail}
            for r in rows
        ]


class BacktestRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def store(self, backtest_id: str, *, strategy: str, strategy_version: str, symbol: str,
                    timeframe: str, start: datetime, end: datetime, kind: str, params: dict,
                    metrics: dict, trades: list[dict], created_at: datetime) -> None:
        async with self.db.session() as s:
            s.add(
                BacktestRow(
                    id=backtest_id, strategy=strategy, strategy_version=strategy_version,
                    symbol=symbol, timeframe=timeframe, start=start, end=end, kind=kind,
                    params=params, metrics=metrics, created_at=created_at,
                )
            )
            for t in trades:
                s.add(BacktestTradeRow(backtest_id=backtest_id, **t))

    async def recent(self, limit: int = 20) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(BacktestRow).order_by(BacktestRow.created_at.desc()).limit(limit)
                )
            ).all()
        return [
            {"id": r.id, "strategy": r.strategy, "symbol": r.symbol, "timeframe": r.timeframe,
             "kind": r.kind, "start": r.start.isoformat(), "end": r.end.isoformat(),
             "metrics": r.metrics}
            for r in rows
        ]


class RetentionService:
    """Deletes expired high-frequency data. Runs daily (see app.main)."""

    def __init__(self, db: Database, *, raw_hf_days: int, candles_1m_days: int,
                 orderbook_days: int, features_days: int) -> None:
        self.db = db
        self.raw_hf_days = raw_hf_days
        self.candles_1m_days = candles_1m_days
        self.orderbook_days = orderbook_days
        self.features_days = features_days

    async def run_once(self, now: datetime) -> dict[str, int]:
        deleted: dict[str, int] = {}
        async with self.db.session() as s:
            res = await s.execute(
                delete(MarketEventRow).where(
                    MarketEventRow.timestamp_event < now - timedelta(days=self.raw_hf_days)
                )
            )
            deleted["market_events"] = res.rowcount or 0
            res = await s.execute(
                delete(TradeRow).where(TradeRow.timestamp < now - timedelta(days=self.raw_hf_days))
            )
            deleted["trades"] = res.rowcount or 0
            res = await s.execute(
                delete(MarketCandleRow).where(
                    MarketCandleRow.timeframe == "1m",
                    MarketCandleRow.open_time < now - timedelta(days=self.candles_1m_days),
                )
            )
            deleted["candles_1m"] = res.rowcount or 0
            res = await s.execute(
                delete(OrderbookSnapshotRow).where(
                    OrderbookSnapshotRow.timestamp < now - timedelta(days=self.orderbook_days)
                )
            )
            deleted["orderbook_snapshots"] = res.rowcount or 0
            res = await s.execute(
                delete(FeatureRow).where(
                    FeatureRow.timestamp < now - timedelta(days=self.features_days)
                )
            )
            deleted["features"] = res.rowcount or 0
        return deleted
