"""SHORT_ONLY mode: message → watch pump → confirmed reversal → SHORT only.

The long leg must never trade in this mode; a message alone must never open a
position — a real pump AND a sustained reversal are both required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.clock import SimClock
from src.core.config import Settings
from src.core.domain import AggTrade, BookTop, SkipReason, TweetEvent, TweetKind
from src.core.state_machine import IllegalTransition, TradeState, TradeStateMachine
from src.exchange.symbol_mapper import SymbolRules
from src.execution.adapters import PaperAdapter
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskManager
from src.storage.database import Repo
from src.strategy.entry import EntryInputs, validate_watch_entry
from src.strategy.session import TradeSession
from tests.test_strategy_units import classification

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
RULES = SymbolRules(symbol="TAOUSDT", base_asset="TAO", tick_size=Decimal("0.01"),
                    step_size=Decimal("0.1"), min_qty=Decimal("0.1"),
                    min_notional=Decimal("5"))


def make_cfg(**kw) -> Settings:
    defaults = dict(STRATEGY_MODE="SHORT_ONLY", MIN_PUMP_PERCENT=1.0,
                    PUMP_WATCH_WINDOW_SECONDS=300)
    defaults.update(kw)
    return Settings(_env_file=None, **defaults)


def tg_tweet(clock, text="$TAO pump inc, bought") -> TweetEvent:
    return TweetEvent(tweet_id="tg:-100:55", author_id="pumpchannel", text=text,
                      kind=TweetKind.ORIGINAL, created_at=clock.now(),
                      received_at=clock.now() + timedelta(seconds=2))


async def build(db, clock, cfg):
    repo = Repo(db)
    kill = KillSwitch()
    risk = RiskManager(cfg=cfg, repo=repo, kill=kill)
    await risk.restore(clock.now())
    session = TradeSession(
        session_id="watch-1", tweet=tg_tweet(clock), classification=classification(),
        rules=RULES, cfg=cfg, clock=clock,
        orders=OrderManager(PaperAdapter(cfg, clock), repo, clock),
        positions=PositionManager(repo, clock, kill), risk=risk, repo=repo)
    return session, repo


def book_at(mid, ts):
    return BookTop(bid_price=mid - 0.02, bid_qty=80, ask_price=mid + 0.02, ask_qty=80,
                   timestamp=ts)


def inputs(clock, mid=100.0, ref=100.0):
    return EntryInputs(now=clock.now(), reference_price=ref, mid_price=mid,
                       spread_pct=0.04, volume_24h_quote=60_000_000.0,
                       bid_liquidity_usdt=90_000.0, ask_liquidity_usdt=90_000.0,
                       feed_staleness_s=0.2)


async def drive(session, clock, ticks):
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


def pump_then_dump(pump_to=102.0, dump_to=99.1):
    ticks = []
    for i in range(30):                     # pump 100 → pump_to over 15s
        ticks.append((i * 0.5, 100.0 + (pump_to - 100.0) * (i / 29), 3.0, False))
    for i in range(60):                     # dump to dump_to over 60s, heavy sells
        ticks.append((16 + i, pump_to - (pump_to - dump_to) * (i / 59), 3.0, True))
    for i in range(20):                     # settle/bounce → TP or trailing stop fires
        ticks.append((77 + i, dump_to + 0.5 * (i / 19), 2.0, False))
    return ticks


class TestWatchValidation:
    def test_pumped_price_is_NOT_rejected(self):
        """No chase filter in short-only: the pump is what we're here to fade."""
        clock = SimClock(T0)
        cfg = make_cfg()
        tweet = tg_tweet(clock)
        d = validate_watch_entry(tweet, classification(), "TAOUSDT",
                                 inputs(clock, mid=103.0, ref=100.0), cfg)
        assert d.approved
        assert d.snapshot.price_change_since_tweet_pct == pytest.approx(3.0)

    def test_quality_gates_still_apply(self):
        clock = SimClock(T0)
        cfg = make_cfg()
        tweet = tg_tweet(clock)
        bad_spread = inputs(clock).model_copy(update={"spread_pct": 0.5})
        assert validate_watch_entry(tweet, classification(), "TAOUSDT", bad_spread,
                                    cfg).skip_reason == SkipReason.SPREAD_TOO_HIGH
        illiquid = inputs(clock).model_copy(update={"volume_24h_quote": 100.0})
        assert validate_watch_entry(tweet, classification(), "TAOUSDT", illiquid,
                                    cfg).skip_reason == SkipReason.LOW_LIQUIDITY

    def test_custom_max_age_for_telegram(self):
        clock = SimClock(T0 + timedelta(seconds=70))
        cfg = make_cfg()
        tweet = TweetEvent(tweet_id="tg:-1:2", author_id="c", text="x $TAO",
                           kind=TweetKind.ORIGINAL, created_at=T0, received_at=T0)
        strict = validate_watch_entry(tweet, classification(), "TAOUSDT",
                                      inputs(clock), cfg, max_age_seconds=45)
        assert strict.skip_reason == SkipReason.TWEET_TOO_OLD
        relaxed = validate_watch_entry(tweet, classification(), "TAOUSDT",
                                       inputs(clock), cfg, max_age_seconds=90)
        assert relaxed.approved


class TestShortOnlyFlow:
    async def test_message_pump_reversal_short_roundtrip(self, db):
        clock = SimClock(T0)
        session, repo = await build(db, clock, make_cfg())
        assert await session.start_watch(inputs(clock))
        assert session.sm.state == TradeState.MONITORING_PUMP

        await drive(session, clock, pump_then_dump())
        assert session.done, f"stuck in {session.sm.state}"
        trades = await repo.closed_trades()
        assert [t.leg for t in trades] == ["SHORT"]     # NO long leg, ever
        short = trades[0]
        assert short.net_pnl > 0
        assert short.close_reason in ("take_profit", "trailing_stop")
        states = [e[0].value for e in session.sm.history]
        assert "MONITORING_PUMP" in states and "LONG_OPEN" not in states

    async def test_no_pump_no_trade(self, db):
        """Message alone must never open a position — flat market → DONE, 0 trades."""
        clock = SimClock(T0)
        session, repo = await build(db, clock, make_cfg(PUMP_WATCH_WINDOW_SECONDS=60))
        assert await session.start_watch(inputs(clock))
        flat = [(i * 2.0, 100.0 + 0.02 * ((i % 3) - 1), 1.0, i % 2 == 0)
                for i in range(40)]
        await drive(session, clock, flat)
        assert session.done and session.sm.state == TradeState.DONE
        assert await repo.closed_trades() == []
        assert "expired" in session.sm.history[-1][1]

    async def test_pump_without_reversal_no_short(self, db):
        """Price pumps and keeps going — never short into strength."""
        clock = SimClock(T0)
        session, repo = await build(db, clock, make_cfg(PUMP_WATCH_WINDOW_SECONDS=90))
        assert await session.start_watch(inputs(clock))
        only_up = [(i, 100.0 * (1 + 0.0006 * i), 3.0, False) for i in range(95)]
        await drive(session, clock, only_up)
        assert session.done
        assert await repo.closed_trades() == []

    async def test_reversal_bounce_rejects_short(self, db):
        """Reversal seen, but price bounces during confirmation → no short."""
        clock = SimClock(T0)
        session, repo = await build(db, clock, make_cfg(MIN_REVERSAL_SCORE=40))
        assert await session.start_watch(inputs(clock))
        ticks = []
        for i in range(30):
            ticks.append((i * 0.5, 100.0 + 2.0 * (i / 29), 3.0, False))
        for i in range(8):    # brief sharp pullback → reversal seen
            ticks.append((16 + i * 0.5, 102.0 - 1.1 * (i / 7), 4.0, True))
        for i in range(60):   # V-bounce inside the confirmation window
            ticks.append((20.5 + i * 0.5, 101.0 + 1.6 * (i / 59), 4.0, False))
        await drive(session, clock, ticks)
        assert session.done
        assert await repo.closed_trades() == []

    async def test_short_stop_loss_when_pump_resumes(self, db):
        """Short opens on confirmed reversal, pump resumes → SL caps the damage."""
        clock = SimClock(T0)
        session, repo = await build(db, clock, make_cfg())
        assert await session.start_watch(inputs(clock))
        ticks = []
        for i in range(30):   # pump to 102
            ticks.append((i * 0.5, 100.0 + 2.0 * (i / 29), 3.0, False))
        for i in range(30):   # convincing dump to 100.9 → short confirms
            ticks.append((16 + i, 102.0 - 1.1 * (i / 29), 3.0, True))
        for i in range(40):   # violent second leg up → stop loss
            ticks.append((47 + i, 100.9 * (1 + 0.0006 * i), 4.0, False))
        await drive(session, clock, ticks)
        assert session.done
        trades = await repo.closed_trades()
        assert len(trades) == 1 and trades[0].leg == "SHORT"
        assert trades[0].close_reason == "stop_loss"
        assert trades[0].net_pnl < 0
        # loss stays within the per-trade risk budget (± fees/slippage)
        assert abs(trades[0].net_pnl) < make_cfg().max_risk_per_trade_usdt * 1.6


class TestStateMachineShortOnly:
    async def test_monitoring_path_legal(self):
        sm = TradeStateMachine("s")
        for state in (TradeState.MARKET_VALIDATION, TradeState.MONITORING_PUMP,
                      TradeState.WAITING_SHORT_CONFIRMATION, TradeState.SHORT_OPEN,
                      TradeState.SHORT_EXIT, TradeState.DONE):
            await sm.to(state, "t", T0)
        assert sm.terminal

    async def test_monitoring_cannot_open_long(self):
        sm = TradeStateMachine("s")
        await sm.to(TradeState.MARKET_VALIDATION, "t", T0)
        await sm.to(TradeState.MONITORING_PUMP, "t", T0)
        with pytest.raises(IllegalTransition):
            await sm.to(TradeState.LONG_OPEN, "t", T0)
