"""Risk engine tests: absolute veto, latching locks, sizing, no-martingale."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config.settings import RiskConfig
from app.core.clock import SimClock
from app.core.state import StateMachine
from app.models.enums import Direction, Venue
from app.models.orders import Position
from app.portfolio.correlation import CorrelationEngine
from app.risk.engine import EntryRequest, RiskEngine
from tests.helpers import make_candles

CFG = RiskConfig(
    max_daily_loss_pct=3.0, max_weekly_loss_pct=6.0, max_drawdown_pct=15.0,
    max_consecutive_losses=3, min_signal_confidence=60.0,
    min_liquidity_quote_vol_24h=1_000_000.0, max_spread_pct=0.10,
    cooldown_after_loss_minutes=30,
)


@pytest.fixture
def engine(sim_clock: SimClock):
    state = StateMachine(clock=sim_clock)
    state.mark_started()
    eng = RiskEngine(cfg=CFG, clock=sim_clock, state=state)
    eng.update_equity(10_000.0)
    return eng


def good_request(**overrides) -> EntryRequest:
    defaults = dict(
        symbol="BTCUSDT", direction=Direction.LONG, entry_price=50_000.0,
        stop_price=49_000.0, signal_confidence=75.0, data_quality=1.0, spread_pct=0.02,
        quote_volume_24h=5_000_000.0, equity=10_000.0, open_positions=[],
    )
    defaults.update(overrides)
    return EntryRequest(**defaults)


def open_position(symbol="ETHUSDT", qty="1", entry="3000") -> Position:
    return Position(venue=Venue.PAPER, symbol=symbol, qty=Decimal(qty),
                    avg_entry_price=Decimal(entry),
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC))


class TestVeto:
    def test_clean_request_approved_with_sizing(self, engine):
        decision = engine.evaluate_entry(good_request())
        assert decision.approved
        # risk-based size would be 1% of 10k / 1000 USDT stop distance = 0.1 BTC (5000 notional),
        # but the 20% max-notional cap binds first: 2000 / 50000 = 0.04 BTC
        assert float(decision.original_quantity) == pytest.approx(0.1, rel=1e-6)
        assert float(decision.approved_quantity) == pytest.approx(0.04, rel=1e-6)

    def test_short_rejected_on_spot(self, engine):
        decision = engine.evaluate_entry(good_request(direction=Direction.SHORT))
        assert not decision.approved and not decision.checks["spot_long_only"]

    def test_low_confidence_rejected(self, engine):
        assert not engine.evaluate_entry(good_request(signal_confidence=50)).approved

    def test_low_data_quality_rejected(self, engine):
        assert not engine.evaluate_entry(good_request(data_quality=0.5)).approved

    def test_wide_spread_rejected(self, engine):
        assert not engine.evaluate_entry(good_request(spread_pct=0.5)).approved

    def test_unknown_spread_rejected_fail_safe(self, engine):
        assert not engine.evaluate_entry(good_request(spread_pct=None)).approved

    def test_illiquid_rejected(self, engine):
        assert not engine.evaluate_entry(good_request(quote_volume_24h=100.0)).approved

    def test_unknown_liquidity_rejected_fail_safe(self, engine):
        assert not engine.evaluate_entry(good_request(quote_volume_24h=None)).approved

    def test_stop_above_entry_rejected(self, engine):
        assert not engine.evaluate_entry(good_request(stop_price=51_000.0)).approved

    def test_max_positions_enforced(self, engine):
        positions = [open_position(s) for s in ("ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT")]
        decision = engine.evaluate_entry(good_request(open_positions=positions))
        assert not decision.approved and not decision.checks["max_positions"]

    def test_paused_system_rejects(self, engine):
        engine.state.pause()
        decision = engine.evaluate_entry(good_request())
        assert not decision.approved and not decision.checks["system_state"]

    def test_stale_data_rejects(self, engine):
        engine.state.set_symbol_stale("BTCUSDT", True)
        assert not engine.evaluate_entry(good_request()).approved

    def test_every_rejection_has_reason(self, engine):
        decision = engine.evaluate_entry(good_request(signal_confidence=10, spread_pct=None))
        assert not decision.approved and len(decision.reasons) >= 2


class TestLocks:
    def test_daily_loss_lock_latches(self, engine, sim_clock):
        engine.on_position_closed(-400.0, equity=9_600.0)  # -4% day > 3% limit
        assert engine.state.status().risk_locked
        # still locked for later entries, cooldown alone isn't the blocker
        sim_clock.advance_to(sim_clock.now() + timedelta(hours=2))
        decision = engine.evaluate_entry(good_request())
        assert not decision.approved and not decision.checks["system_state"]

    def test_operator_unlock_clears(self, engine, sim_clock):
        engine.on_position_closed(-400.0, equity=9_600.0)
        sim_clock.advance_to(sim_clock.now() + timedelta(hours=1))
        engine.operator_unlock()
        assert engine.evaluate_entry(good_request()).approved

    def test_daily_pnl_resets_next_day_but_lock_stays(self, engine, sim_clock):
        engine.on_position_closed(-400.0, equity=9_600.0)
        sim_clock.advance_to(sim_clock.now() + timedelta(days=1))
        engine._roll_periods(sim_clock.now())
        assert engine.realized_pnl_today == 0.0
        assert engine.state.status().risk_locked  # latched until operator unlock

    def test_drawdown_lock(self, engine):
        engine.update_equity(10_000.0)
        engine.update_equity(8_000.0)  # -20% > 15%
        assert engine.state.status().risk_locked

    def test_weekly_loss_lock(self, engine, sim_clock):
        engine.on_position_closed(-250.0, equity=9_750.0)
        engine.operator_unlock()  # not locked by daily (2.5% < 3%)
        sim_clock.advance_to(sim_clock.now() + timedelta(days=1))
        engine.on_position_closed(-250.0, equity=9_500.0)
        sim_clock.advance_to(sim_clock.now() + timedelta(days=1))
        engine.on_position_closed(-200.0, equity=9_300.0)  # week total -7% > 6%
        assert engine.state.status().risk_locked


class TestCooldownsAndStreaks:
    def test_cooldown_after_loss(self, engine, sim_clock):
        engine.on_position_closed(-50.0, equity=9_950.0)
        decision = engine.evaluate_entry(good_request())
        assert not decision.approved and not decision.checks["cooldown"]
        sim_clock.advance_to(sim_clock.now() + timedelta(minutes=31))
        assert engine.evaluate_entry(good_request()).approved

    def test_consecutive_loss_lockout(self, engine, sim_clock):
        for _ in range(3):
            engine.on_position_closed(-10.0, equity=10_000.0)
            sim_clock.advance_to(sim_clock.now() + timedelta(hours=1))
        decision = engine.evaluate_entry(good_request())
        assert not decision.approved and not decision.checks["consecutive_losses"]

    def test_win_resets_streak(self, engine, sim_clock):
        engine.on_position_closed(-10.0, equity=10_000.0)
        engine.on_position_closed(-10.0, equity=10_000.0)
        engine.on_position_closed(+50.0, equity=10_030.0)
        assert engine.consecutive_losses == 0

    def test_vol_shock_cooldown(self, engine, sim_clock):
        engine.on_volatility_shock("BTCUSDT", move_pct=7.0)
        assert not engine.evaluate_entry(good_request()).approved


class TestSizingProperties:
    def test_no_martingale_size_shrinks_with_equity(self, engine, sim_clock):
        big = engine.evaluate_entry(good_request(equity=10_000.0))
        small = engine.evaluate_entry(good_request(equity=5_000.0))
        assert float(small.approved_quantity) < float(big.approved_quantity)

    def test_notional_cap_applies_without_stop(self, engine):
        decision = engine.evaluate_entry(good_request(stop_price=None))
        notional = float(decision.approved_quantity) * 50_000.0
        assert decision.approved
        assert notional <= 10_000.0 * CFG.max_position_notional_pct / 100.0 + 1e-6

    def test_asset_exposure_headroom_limits_size(self, engine):
        # existing BTC position consumes most of the 25% per-asset budget
        existing = open_position("BTCUSDT", qty="0.04", entry="50000")  # 2000 notional
        decision = engine.evaluate_entry(good_request(
            open_positions=[existing], mark_prices={"BTCUSDT": 50_000.0}))
        if decision.approved:
            new_notional = float(decision.approved_quantity) * 50_000.0
            assert new_notional + 2_000.0 <= 10_000.0 * CFG.max_exposure_per_asset_pct / 100.0 + 1e-6


class TestCorrelatedExposure:
    def test_unknown_correlation_fails_safe(self, engine):
        # without a correlation engine every open position counts as correlated
        positions = [open_position("ETHUSDT", qty="1", entry="3000"),
                     open_position("SOLUSDT", qty="20", entry="150")]  # 6000 notional
        decision = engine.evaluate_entry(good_request(
            open_positions=positions,
            mark_prices={"ETHUSDT": 3000.0, "SOLUSDT": 150.0}))
        assert not decision.approved and not decision.checks["correlated_exposure"]

    def test_uncorrelated_assets_pass(self, sim_clock):
        state = StateMachine(clock=sim_clock)
        state.mark_started()
        corr = CorrelationEngine(window=100, min_overlap=50)
        up = make_candles(200, drift=0.002, vol=0.001, seed=1, symbol="ETHUSDT")
        down = make_candles(200, drift=-0.002, vol=0.001, seed=2, symbol="BTCUSDT")
        corr.update("ETHUSDT", up)
        corr.update("BTCUSDT", down)
        assert corr.correlation("BTCUSDT", "ETHUSDT") < 0.5
        eng = RiskEngine(cfg=CFG, clock=sim_clock, state=state, correlations=corr)
        eng.update_equity(10_000.0)
        positions = [open_position("ETHUSDT", qty="2", entry="3000")]  # 6000 notional
        decision = eng.evaluate_entry(good_request(
            open_positions=positions, mark_prices={"ETHUSDT": 3000.0}))
        assert decision.approved  # ETH position isn't correlated with BTC here
