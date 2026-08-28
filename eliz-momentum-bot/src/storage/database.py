"""Async DB engine + repository (the only SQL in the project)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain import (
    Classification,
    MarketSnapshot,
    OrderIntent,
    OrderResult,
    OrderStatus,
    PositionRecord,
    SkipReason,
    TweetEvent,
)
from src.core.logger import get_logger
from src.storage.models import (
    Base,
    DailyStatsRow,
    MarketSnapshotRow,
    OrderRow,
    PositionRow,
    SignalRow,
    StrategyEventRow,
    TradeRow,
    TweetRow,
)

log = get_logger(__name__)


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite"):
            db_path = url.split("///")[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def healthcheck(self) -> bool:
        try:
            from sqlalchemy import text

            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── tweets / signals ─────────────────────────────────────────────────────

    async def store_tweet(self, t: TweetEvent) -> bool:
        """False → duplicate tweet_id (already processed)."""
        try:
            async with self.db.session() as s:
                s.add(TweetRow(tweet_id=t.tweet_id, author_id=t.author_id, text=t.text,
                               kind=t.kind.value, created_at=t.created_at,
                               received_at=t.received_at, twitter_latency_ms=t.latency_ms,
                               raw=t.raw))
            return True
        except IntegrityError:
            return False

    async def store_signal(self, c: Classification, now: datetime, *,
                           session_id: str | None = None, skipped: bool = False,
                           skip_reason: SkipReason | None = None) -> None:
        try:
            async with self.db.session() as s:
                s.add(SignalRow(
                    tweet_id=c.tweet_id, session_id=session_id,
                    is_trade_signal=c.is_trade_signal, symbol=c.symbol,
                    direction=c.direction.value, confidence=c.confidence,
                    signal_stage=c.signal_stage.value, action=c.action.value,
                    reason=c.reason, matched_phrases=c.matched_phrases, used_llm=c.used_llm,
                    classification_latency_ms=c.classification_latency_ms,
                    skipped=skipped, skip_reason=skip_reason.value if skip_reason else None,
                    created_at=now))
        except IntegrityError:
            log.warning("signal for tweet %s already stored", c.tweet_id)

    async def mark_signal_skipped(self, tweet_id: str, reason: SkipReason) -> None:
        async with self.db.session() as s:
            await s.execute(update(SignalRow).where(SignalRow.tweet_id == tweet_id)
                            .values(skipped=True, skip_reason=reason.value))

    async def store_snapshot(self, session_id: str, snap: MarketSnapshot, label: str) -> None:
        async with self.db.session() as s:
            s.add(MarketSnapshotRow(
                session_id=session_id, symbol=snap.symbol, timestamp=snap.timestamp,
                label=label, reference_price=snap.reference_price,
                current_price=snap.current_price,
                price_change_since_tweet_pct=snap.price_change_since_tweet_pct,
                spread_pct=snap.spread_pct, volume_24h_quote=snap.volume_24h_quote,
                bid_liquidity_usdt=snap.bid_liquidity_usdt,
                ask_liquidity_usdt=snap.ask_liquidity_usdt))

    # ── orders / positions / trades ──────────────────────────────────────────

    async def store_order_intent(self, intent: OrderIntent, client_order_id: str,
                                 requested_price: float | None, now: datetime) -> None:
        async with self.db.session() as s:
            s.add(OrderRow(
                id=intent.id, session_id=intent.session_id, client_order_id=client_order_id,
                symbol=intent.symbol, side=intent.side.value, leg=intent.leg,
                order_type=intent.order_type, quantity=intent.quantity,
                limit_price=intent.limit_price, reduce_only=intent.reduce_only,
                status=OrderStatus.PENDING_SUBMIT.value, requested_price=requested_price,
                created_at=now, updated_at=now))

    async def update_order_result(self, result: OrderResult, now: datetime) -> None:
        async with self.db.session() as s:
            await s.execute(update(OrderRow).where(OrderRow.id == result.intent_id).values(
                status=result.status.value, exchange_order_id=result.exchange_order_id,
                executed_price=result.executed_price, executed_qty=result.executed_qty,
                fee_usdt=result.fee_usdt, slippage_pct=result.slippage_pct,
                order_latency_ms=result.order_latency_ms, error=result.error, updated_at=now))

    async def get_order_by_client_id(self, client_order_id: str) -> OrderRow | None:
        async with self.db.session() as s:
            return await s.scalar(select(OrderRow).where(OrderRow.client_order_id == client_order_id))

    async def orders_with_status(self, statuses: list[str]) -> list[OrderRow]:
        async with self.db.session() as s:
            return list((await s.scalars(select(OrderRow).where(OrderRow.status.in_(statuses)))).all())

    async def store_position(self, p: PositionRecord) -> None:
        async with self.db.session() as s:
            s.add(PositionRow(id=p.id, session_id=p.session_id, symbol=p.symbol,
                              side=p.side.value, qty=p.qty, entry_price=p.entry_price,
                              opened_at=p.opened_at, closed_at=p.closed_at,
                              exit_price=p.exit_price, realized_pnl=p.realized_pnl,
                              fees=p.fees, close_reason=p.close_reason))

    async def update_position(self, p: PositionRecord) -> None:
        async with self.db.session() as s:
            await s.execute(update(PositionRow).where(PositionRow.id == p.id).values(
                qty=p.qty, closed_at=p.closed_at, exit_price=p.exit_price,
                realized_pnl=p.realized_pnl, fees=p.fees, close_reason=p.close_reason))

    async def open_positions(self) -> list[PositionRow]:
        async with self.db.session() as s:
            return list((await s.scalars(
                select(PositionRow).where(PositionRow.closed_at.is_(None)))).all())

    async def store_trade(self, **kw) -> None:
        async with self.db.session() as s:
            s.add(TradeRow(**kw))

    async def update_trade(self, trade_id: str, **kw) -> None:
        async with self.db.session() as s:
            await s.execute(update(TradeRow).where(TradeRow.id == trade_id).values(**kw))

    async def closed_trades(self, since: datetime | None = None) -> list[TradeRow]:
        stmt = select(TradeRow).where(TradeRow.closed_at.is_not(None))
        if since is not None:
            stmt = stmt.where(TradeRow.closed_at >= since)
        async with self.db.session() as s:
            return list((await s.scalars(stmt.order_by(TradeRow.closed_at))).all())

    # ── strategy events / daily stats ────────────────────────────────────────

    async def store_event(self, session_id: str, from_state: str, to_state: str,
                          reason: str, now: datetime, payload: dict | None = None) -> None:
        async with self.db.session() as s:
            s.add(StrategyEventRow(session_id=session_id, timestamp=now,
                                   from_state=from_state, to_state=to_state, reason=reason,
                                   payload=payload or {}))

    async def get_daily(self, day: str) -> DailyStatsRow | None:
        async with self.db.session() as s:
            return await s.get(DailyStatsRow, day)

    async def upsert_daily(self, day: str, *, trades_delta: int = 0, pnl_delta: float = 0.0,
                           fees_delta: float = 0.0, consecutive_losses: int | None = None,
                           kill_switch: bool | None = None, now: datetime | None = None) -> DailyStatsRow:
        async with self.db.session() as s:
            row = await s.get(DailyStatsRow, day)
            if row is None:
                row = DailyStatsRow(day=day, updated_at=now)
                s.add(row)
            row.trades += trades_delta
            row.realized_pnl += pnl_delta
            row.fees += fees_delta
            if consecutive_losses is not None:
                row.consecutive_losses = consecutive_losses
            if kill_switch is not None:
                row.kill_switch_fired = kill_switch
            row.updated_at = now
            return row
