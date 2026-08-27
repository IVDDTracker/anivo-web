"""Data quality: staleness gating and abnormal-price quarantine (chaos: 50% anomaly)."""

from __future__ import annotations

from datetime import timedelta

from app.core.state import StateMachine
from app.data.quality import DataQualityService


def make(sim_clock):
    state = StateMachine(clock=sim_clock)
    state.mark_started()
    return DataQualityService(clock=sim_clock, state=state, max_age_s=120,
                              abnormal_jump_pct=20.0, confirm_ticks=3), state


def test_normal_prices_accepted(sim_clock):
    dq, _ = make(sim_clock)
    assert dq.on_price("BTCUSDT", 50000, sim_clock.now())
    assert dq.on_price("BTCUSDT", 50100, sim_clock.now())
    assert dq.quality_score("BTCUSDT") > 0.9


def test_50pct_anomaly_quarantines_symbol(sim_clock):
    dq, _ = make(sim_clock)
    dq.on_price("BTCUSDT", 50000, sim_clock.now())
    assert not dq.on_price("BTCUSDT", 75000, sim_clock.now())  # +50% instant → rejected
    assert dq.quality_score("BTCUSDT") == 0.0
    assert dq.last_price("BTCUSDT") is None  # quarantined symbol exposes no price


def test_quarantine_lifts_after_consistent_ticks(sim_clock):
    dq, _ = make(sim_clock)
    dq.on_price("BTCUSDT", 50000, sim_clock.now())
    dq.on_price("BTCUSDT", 75000, sim_clock.now())  # genuine violent repricing begins
    assert not dq.on_price("BTCUSDT", 75100, sim_clock.now())
    accepted = dq.on_price("BTCUSDT", 75050, sim_clock.now())
    assert accepted  # third consistent tick confirms the new level
    assert dq.quality_score("BTCUSDT") > 0.0
    assert dq.last_price("BTCUSDT") == 75050


def test_flapping_prices_stay_quarantined(sim_clock):
    dq, _ = make(sim_clock)
    dq.on_price("BTCUSDT", 50000, sim_clock.now())
    dq.on_price("BTCUSDT", 75000, sim_clock.now())
    assert not dq.on_price("BTCUSDT", 30000, sim_clock.now())  # still insane
    assert not dq.on_price("BTCUSDT", 90000, sim_clock.now())
    assert dq.quality_score("BTCUSDT") == 0.0


def test_staleness_marks_state_machine(sim_clock):
    dq, state = make(sim_clock)
    dq.on_price("BTCUSDT", 50000, sim_clock.now())
    dq.check_freshness()
    assert state.can_open_new_positions("BTCUSDT")[0]
    sim_clock.advance_to(sim_clock.now() + timedelta(seconds=300))
    dq.check_freshness()
    ok, reason = state.can_open_new_positions("BTCUSDT")
    assert not ok and "stale" in reason
    assert dq.quality_score("BTCUSDT") == 0.0


def test_unknown_symbol_scores_zero(sim_clock):
    dq, _ = make(sim_clock)
    assert dq.quality_score("DOGEUSDT") == 0.0
