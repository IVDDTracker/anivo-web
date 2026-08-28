"""Integration (spec §19): fake tweet → classifier → paper exchange → LONG →
simulated pump → reversal detected → SHORT confirmed → SHORT exit → DONE.

Drives the REAL TradeSession with the REAL paper adapter over a synthetic tick
sequence on a SimClock — the exact code path live trading uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.clock import SimClock
from src.core.config import Settings
from src.core.domain import AggTrade, BookTop, SkipReason, TweetEvent, TweetKind
from src.core.state_machine import TradeState
from src.exchange.symbol_mapper import SymbolRules
from src.execution.adapters import PaperAdapter
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskManager
from src.storage.database import Repo
from src.storage.models import TradeRow
from src.strategy.entry import EntryInputs
from src.strategy.session import TradeSession
from src.twitter.classifier import SignalClassifier
from src.twitter.parser import extract_candidates

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
RULES = SymbolRules(symbol="TAOUSDT", base_asset="TAO", tick_size=Decimal("0.01"),
                    step_size=Decimal("0.1"), min_qty=Decimal("0.1"),
                    min_notional=Decimal("5"))


def make_cfg(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


async def build_session(db, clock, cfg, tweet_text="Bought $TAO here"):
    repo = Repo(db)
    tweet = TweetEvent(tweet_id="900", author_id="1", text=tweet_text,
                       kind=TweetKind.ORIGINAL, created_at=clock.now(),
                       received_at=clock.now() + timedelta(milliseconds=800))
    classification = await SignalClassifier().classify(
        tweet, extract_candidates(tweet_text, {"TAO"}))
    assert classification.is_trade_signal
    kill = KillSwitch()
    risk = RiskManager(cfg=cfg, repo=repo, kill=kill)
    await risk.restore(clock.now())
    session = TradeSession(
        session_id="sess-1", tweet=tweet, classification=classification, rules=RULES,
        cfg=cfg, clock=clock, orders=OrderManager(PaperAdapter(cfg, clock), repo, clock),
        positions=PositionManager(repo, clock, kill), risk=risk, repo=repo)
    return session, repo, risk


def book_at(mid: float, ts) -> BookTop:
    return BookTop(bid_price=mid - 0.02, bid_qty=80, ask_price=mid + 0.02, ask_qty=80,
                   timestamp=ts)


async def drive(session: TradeSession, clock: SimClock, ticks) -> None:
    """ticks: iterable of (seconds_offset_from_start, price, qty, is_sell)."""
    start = clock.now()
    for sec, price, qty, sell in ticks:
        when = start + timedelta(seconds=sec)
        if when > clock.now():
            clock.advance_to(when)
        await session.on_book(book_at(price, when))
        await session.on_trade(AggTrade(price=price, qty=qty, timestamp=when,
                                        is_buyer_maker=sell))
        if session.done:
            return


def pump_then_dump():
    ticks = []
    # pump: 100 → 102 over 15s, heavy aggressive buying
    for i in range(30):
        ticks.append((i * 0.5, 100.0 + 2.0 * (i / 29), 3.0, False))
    # stall + dump: fades to 100.4 on heavy selling over the next 40s
    for i in range(40):
        ticks.append((16 + i, 102.0 - 1.6 * (i / 39), 3.0, True))
    # continued slide for the short leg to profit: 100.4 → 98.2
    for i in range(40):
        ticks.append((57 + i, 100.4 - 2.2 * (i / 39), 2.0, True))
    return ticks


class TestFullFlow:
    async def test_tweet_to_short_roundtrip(self, db):
        clock = SimClock(T0)
        cfg = make_cfg()
        session, repo, risk = await build_session(db, clock, cfg)
        await session.on_book(book_at(100.0, clock.now()))
        ok = await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=100.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        assert ok and session.sm.state == TradeState.LONG_OPEN

        await drive(session, clock, pump_then_dump())
        assert session.done, f"stuck in {session.sm.state}"
        assert session.sm.state == TradeState.DONE

        trades = await repo.closed_trades()
        legs = {t.leg: t for t in trades}
        assert set(legs) == {"LONG", "SHORT"}, f"legs: {list(legs)}"
        long_leg, short_leg = legs["LONG"], legs["SHORT"]
        assert long_leg.net_pnl > 0                      # rode the pump
        assert long_leg.peak_price == pytest.approx(102.0, abs=0.1)
        assert long_leg.reversal_score_at_exit >= cfg.min_reversal_score
        assert "reversal_score" in long_leg.close_reason
        assert short_leg.net_pnl > 0                     # rode the dump
        assert short_leg.close_reason in ("take_profit", "trailing_stop")
        # state trail persisted for every transition
        states = [e.to_state for e in await _events(repo, "sess-1")]
        assert states[:4] == ["MARKET_VALIDATION", "ENTRY_APPROVED", "LONG_OPEN",
                              "LONG_EXIT"]
        assert "SHORT_OPEN" in states and states[-1] == "DONE"

    async def test_long_stop_loss_no_short(self, db):
        """Instant dump below the hard stop → protective exit, NO short leg."""
        clock = SimClock(T0)
        session, repo, _ = await build_session(db, clock, make_cfg())
        await session.on_book(book_at(100.0, clock.now()))
        assert await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=100.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        crash = [(1 + i * 0.5, 100.0 - 2.5 * (i / 9), 2.0, True) for i in range(10)]
        await drive(session, clock, crash)
        assert session.done
        trades = await repo.closed_trades()
        assert len(trades) == 1 and trades[0].leg == "LONG"
        assert trades[0].close_reason == "stop_loss"
        assert trades[0].net_pnl < 0

    async def test_bounce_after_long_exit_prevents_short(self, db):
        """Reversal closes the long, but price bounces → short must NOT open.

        Uses a lower (configurable) reversal threshold so the moderate pullback
        exits the long; the immediate bounce must then veto the short leg."""
        clock = SimClock(T0)
        session, repo, _ = await build_session(db, clock, make_cfg(MIN_REVERSAL_SCORE=40))
        await session.on_book(book_at(100.0, clock.now()))
        assert await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=100.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        ticks = []
        for i in range(30):  # pump
            ticks.append((i * 0.5, 100.0 + 2.0 * (i / 29), 3.0, False))
        for i in range(8):   # sharp fast pullback → reversal exit fires mid-way
            ticks.append((16 + i * 0.5, 102.0 - 1.1 * (i / 7), 4.0, True))
        for i in range(40):  # immediate V-shaped bounce INSIDE the confirmation window
            ticks.append((20.5 + i * 0.5, 101.0 + 1.4 * (i / 39), 4.0, False))
        await drive(session, clock, ticks)
        assert session.done
        trades = await repo.closed_trades()
        assert [t.leg for t in trades] == ["LONG"]  # no short leg

    async def test_pumped_price_skips_entry(self, db):
        clock = SimClock(T0)
        session, repo, _ = await build_session(db, clock, make_cfg())
        await session.on_book(book_at(103.0, clock.now()))
        ok = await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=103.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        assert not ok and session.sm.state == TradeState.SKIPPED

    async def test_kill_switch_blocks_entry(self, db):
        clock = SimClock(T0)
        session, repo, risk = await build_session(db, clock, make_cfg())
        risk.kill.trip("BINANCE_WS_DISCONNECTED", "test")
        await session.on_book(book_at(100.0, clock.now()))
        ok = await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=100.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        assert not ok
        signals_skip = SkipReason.KILL_SWITCH
        assert session.sm.history[-1][1].startswith(str(signals_skip))


async def _events(repo: Repo, session_id: str):
    from sqlalchemy import select

    from src.storage.models import StrategyEventRow

    async with repo.db.session() as s:
        return list((await s.scalars(
            select(StrategyEventRow).where(StrategyEventRow.session_id == session_id)
            .order_by(StrategyEventRow.id))).all())


class TestLatencyMetrics:
    async def test_all_latencies_recorded(self, db):
        clock = SimClock(T0)
        session, repo, _ = await build_session(db, clock, make_cfg())
        await session.on_book(book_at(100.0, clock.now()))
        await session.start(EntryInputs(
            now=clock.now(), reference_price=100.0, mid_price=100.0, spread_pct=0.04,
            volume_24h_quote=60_000_000.0, bid_liquidity_usdt=90_000.0,
            ask_liquidity_usdt=90_000.0, feed_staleness_s=0.2))
        for key in ("twitter_latency_ms", "classification_latency_ms",
                    "binance_order_latency_ms", "total_signal_to_order_latency_ms"):
            assert key in session.latencies
        assert session.latencies["twitter_latency_ms"] == pytest.approx(800.0)


def test_traderow_columns_cover_spec():
    cols = {c.name for c in TradeRow.__table__.columns}
    assert {"gross_pnl", "fees", "net_pnl", "slippage_cost", "peak_price",
            "reversal_score_at_exit"} <= cols
