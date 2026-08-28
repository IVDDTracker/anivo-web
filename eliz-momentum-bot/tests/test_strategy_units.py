"""Unit tests: momentum, reversal score, entry filter, exits, state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.config import ReversalParams, ReversalWeights, Settings, ShortParams
from src.core.domain import (
    AggTrade,
    Classification,
    Direction,
    SignalAction,
    SignalStage,
    SkipReason,
    TweetEvent,
    TweetKind,
)
from src.core.state_machine import IllegalTransition, TradeState, TradeStateMachine
from src.strategy.entry import EntryInputs, validate_entry
from src.strategy.exit import ShortConfirmation, ShortLegManager
from src.strategy.momentum import MomentumTracker
from src.strategy.reversal import ReversalScorer

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CFG = Settings(_env_file=None)


def trade(price: float, sec: float, *, qty=1.0, sell=False) -> AggTrade:
    return AggTrade(price=price, qty=qty, timestamp=T0 + timedelta(seconds=sec),
                    is_buyer_maker=sell)


async def feed_pump(tracker: MomentumTracker, *, start=100.0, peak=102.0, n=40,
                    t_start=0.0, t_end=20.0):
    for i in range(n):
        f = i / (n - 1)
        await tracker.on_trade(trade(start + (peak - start) * f,
                                     t_start + (t_end - t_start) * f, qty=2.0))


class TestMomentumTracker:
    async def test_peak_and_drawdown(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0)
        await feed_pump(tr)
        await tr.on_trade(trade(101.0, 25.0))
        m = tr.metrics(T0 + timedelta(seconds=25))
        assert m.peak_price == pytest.approx(102.0)
        assert m.drawdown_from_peak_pct == pytest.approx((102 - 101) / 102 * 100, rel=1e-6)
        assert m.peak_gain_pct == pytest.approx(2.0, rel=1e-6)
        assert m.seconds_since_new_high > 0

    async def test_flow_and_vwap(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0, flow_window_s=10)
        await tr.on_trade(trade(100, 1, qty=3.0))            # aggressive buy
        await tr.on_trade(trade(100, 2, qty=1.0, sell=True))  # aggressive sell
        m = tr.metrics(T0 + timedelta(seconds=3))
        assert m.buy_share == pytest.approx(0.75)
        assert m.vwap_since_entry == pytest.approx(100.0)

    async def test_velocity_ratio_drops_when_trades_fade(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0, velocity_window_s=20)
        for i in range(20):
            await tr.on_trade(trade(100.5, i * 0.5))  # dense early trades (0-10s)
        m = tr.metrics(T0 + timedelta(seconds=20))    # nothing in the last 10s
        assert m.velocity_ratio < 0.2


class TestReversalScore:
    def make(self, **weight_overrides):
        return ReversalScorer(ReversalWeights(**weight_overrides), ReversalParams())

    async def test_gated_before_any_peak(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0)
        await tr.on_trade(trade(100.05, 1))
        reading = self.make().score(tr.metrics(T0 + timedelta(seconds=2)))
        assert reading.gated and reading.score == 0.0

    async def test_healthy_pump_scores_low(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0)
        await feed_pump(tr)
        reading = self.make().score(tr.metrics(T0 + timedelta(seconds=20)))
        assert not reading.gated
        assert reading.score < 40

    async def test_clear_reversal_scores_high(self):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0)
        await feed_pump(tr, t_end=10.0)
        # dump: price retraces hard on heavy selling, book flips to ask side
        for i in range(20):
            await tr.on_trade(trade(102.0 - i * 0.06, 11 + i * 0.5, qty=3.0, sell=True))
        await tr.on_depth([[100.5, 1.0]], [[100.6, 9.0]], T0 + timedelta(seconds=21))
        reading = self.make().score(tr.metrics(T0 + timedelta(seconds=21)))
        assert reading.score >= 65
        assert reading.components["pullback_from_peak"] > 0.5
        assert reading.components["flow_reversal"] == 1.0

    async def test_score_is_behavior_not_timer(self):
        """Same elapsed time, different behavior → different score (spec §8)."""
        healthy = MomentumTracker(entry_price=100.0, entry_time=T0)
        await feed_pump(healthy, t_end=30.0)
        dumping = MomentumTracker(entry_price=100.0, entry_time=T0)
        await feed_pump(dumping, t_end=15.0)
        for i in range(15):
            await tradehelper(dumping, 102.0 - i * 0.08, 16 + i, sell=True)
        now = T0 + timedelta(seconds=31)
        assert self.make().score(dumping.metrics(now)).score > \
               self.make().score(healthy.metrics(now)).score + 20


async def tradehelper(tr, price, sec, sell=False):
    await tr.on_trade(trade(price, sec, qty=2.0, sell=sell))


def tweet_at(created_sec_ago: float, now: datetime) -> TweetEvent:
    created = now - timedelta(seconds=created_sec_ago)
    return TweetEvent(tweet_id="7", author_id="1", text="bought $TAO",
                      kind=TweetKind.ORIGINAL, created_at=created, received_at=now)


def classification(conf=0.8, stage=SignalStage.CONFIRMED) -> Classification:
    return Classification(is_trade_signal=True, symbol="TAO", direction=Direction.LONG,
                          confidence=conf, signal_stage=stage, action=SignalAction.BUY,
                          tweet_id="7")


def entry_inputs(now, **kw) -> EntryInputs:
    defaults = dict(now=now, reference_price=100.0, mid_price=100.3, spread_pct=0.03,
                    volume_24h_quote=50_000_000.0, bid_liquidity_usdt=100_000.0,
                    ask_liquidity_usdt=100_000.0, feed_staleness_s=0.5)
    defaults.update(kw)
    return EntryInputs(**defaults)


class TestEntryFilter:
    def test_clean_entry_approved(self):
        now = T0
        d = validate_entry(tweet_at(5, now), classification(), "TAOUSDT",
                           entry_inputs(now), CFG)
        assert d.approved and d.snapshot.price_change_since_tweet_pct == pytest.approx(0.3)

    @pytest.mark.parametrize("kw,reason", [
        (dict(mid_price=102.5), SkipReason.PRICE_ALREADY_PUMPED),   # +2.5% > 1.5% chase
        (dict(spread_pct=0.5), SkipReason.SPREAD_TOO_HIGH),
        (dict(volume_24h_quote=1_000.0), SkipReason.LOW_LIQUIDITY),
        (dict(bid_liquidity_usdt=100.0), SkipReason.LOW_LIQUIDITY),
        (dict(feed_staleness_s=30.0), SkipReason.DATA_STALE),
    ])
    def test_market_filters(self, kw, reason):
        d = validate_entry(tweet_at(5, T0), classification(), "TAOUSDT",
                           entry_inputs(T0, **kw), CFG)
        assert not d.approved and d.skip_reason == reason

    def test_old_tweet_rejected(self):
        d = validate_entry(tweet_at(120, T0), classification(), "TAOUSDT",
                           entry_inputs(T0), CFG)
        assert d.skip_reason == SkipReason.TWEET_TOO_OLD

    def test_low_confidence_rejected(self):
        d = validate_entry(tweet_at(5, T0), classification(conf=0.2), "TAOUSDT",
                           entry_inputs(T0), CFG)
        assert d.skip_reason == SkipReason.LOW_CONFIDENCE

    def test_early_signal_gated_by_config(self):
        d = validate_entry(tweet_at(5, T0), classification(stage=SignalStage.EARLY),
                           "TAOUSDT", entry_inputs(T0), CFG)
        assert d.skip_reason == SkipReason.EARLY_SIGNAL_DISABLED
        cfg_on = Settings(_env_file=None, TRADE_EARLY_SIGNALS=True)
        d2 = validate_entry(tweet_at(5, T0), classification(conf=0.7, stage=SignalStage.EARLY),
                            "TAOUSDT", entry_inputs(T0), cfg_on)
        assert d2.approved


class TestShortLeg:
    def make(self):
        return ShortLegManager(entry_price=100.0, entry_time=T0,
                               params=ShortParams(), max_holding_seconds=900)

    def test_stop_loss(self):
        assert self.make().check(101.1, T0 + timedelta(seconds=5)) == "stop_loss"

    def test_take_profit(self):
        assert self.make().check(98.4, T0 + timedelta(seconds=5)) == "take_profit"

    def test_trailing_arms_then_stops(self):
        mgr = self.make()
        assert mgr.check(99.4, T0 + timedelta(seconds=5)) is None  # -0.6% → trailing armed
        assert mgr.trailing_armed
        assert mgr.check(100.05, T0 + timedelta(seconds=8)) == "trailing_stop"

    def test_max_holding(self):
        assert self.make().check(99.9, T0 + timedelta(seconds=1000)) == "max_holding_time"


class TestShortConfirmation:
    async def _reading(self, score: float):
        from src.strategy.reversal import ReversalReading

        return ReversalReading(score=score, components={}, gated=False)

    async def _metrics(self, price: float, vwap_pct: float):
        tr = MomentumTracker(entry_price=100.0, entry_time=T0)
        await tr.on_trade(trade(price, 1))
        m = tr.metrics(T0 + timedelta(seconds=2))
        return m.model_copy(update={"price_vs_vwap_pct": vwap_pct})

    async def test_sustained_reversal_confirms(self):
        sc = ShortConfirmation(cfg=CFG, long_exit_price=101.0, started_at=T0)
        m = await self._metrics(100.8, -0.2)
        assert sc.evaluate(await self._reading(80), m, T0 + timedelta(seconds=1)) == "wait"
        assert sc.evaluate(await self._reading(80), m,
                           T0 + timedelta(seconds=1 + CFG.short_confirmation_seconds)) == "confirm"

    async def test_bounce_rejects(self):
        sc = ShortConfirmation(cfg=CFG, long_exit_price=100.0, started_at=T0)
        m = await self._metrics(100.8, -0.2)  # +0.8% bounce > 0.5% max
        assert sc.evaluate(await self._reading(90), m, T0 + timedelta(seconds=2)) == "reject"

    async def test_timeout_rejects(self):
        sc = ShortConfirmation(cfg=CFG, long_exit_price=101.0, started_at=T0)
        m = await self._metrics(100.8, -0.2)
        late = T0 + timedelta(seconds=CFG.short_confirmation_window_s + 1)
        assert sc.evaluate(await self._reading(40), m, late) == "reject"

    async def test_interrupted_streak_restarts(self):
        sc = ShortConfirmation(cfg=CFG, long_exit_price=101.0, started_at=T0)
        m = await self._metrics(100.8, -0.2)
        assert sc.evaluate(await self._reading(80), m, T0 + timedelta(seconds=1)) == "wait"
        assert sc.evaluate(await self._reading(30), m, T0 + timedelta(seconds=3)) == "wait"
        # score recovered but the sustain clock restarted
        assert sc.evaluate(await self._reading(80), m, T0 + timedelta(seconds=4)) == "wait"


class TestStateMachine:
    async def test_happy_path(self):
        sm = TradeStateMachine("s1")
        now = T0
        for state in (TradeState.MARKET_VALIDATION, TradeState.ENTRY_APPROVED,
                      TradeState.LONG_OPEN, TradeState.LONG_EXIT,
                      TradeState.WAITING_SHORT_CONFIRMATION, TradeState.SHORT_OPEN,
                      TradeState.SHORT_EXIT, TradeState.DONE):
            await sm.to(state, "test", now)
        assert sm.terminal

    async def test_short_without_long_exit_is_illegal(self):
        sm = TradeStateMachine("s1")
        await sm.to(TradeState.MARKET_VALIDATION, "t", T0)
        await sm.to(TradeState.ENTRY_APPROVED, "t", T0)
        await sm.to(TradeState.LONG_OPEN, "t", T0)
        with pytest.raises(IllegalTransition):
            await sm.to(TradeState.SHORT_OPEN, "t", T0)

    async def test_terminal_states_are_final(self):
        sm = TradeStateMachine("s1")
        await sm.to(TradeState.SKIPPED, "t", T0)
        with pytest.raises(IllegalTransition):
            await sm.to(TradeState.MARKET_VALIDATION, "t", T0)
