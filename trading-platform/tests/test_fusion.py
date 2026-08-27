"""Fusion tests: gates are multiplicative — bad data/regime cannot be averaged away."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.enums import Direction, Regime
from app.models.signals import Signal
from app.signals.fusion import FusionInputs, fuse


def _signal(direction=Direction.LONG) -> Signal:
    return Signal(
        symbol="BTCUSDT", strategy="test", strategy_version="1.0", direction=direction,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), reference_price=50_000.0, confidence=70,
    )


BULL_FEATURES = {
    "ema_structure_bull": 1.0, "macd_hist": 5.0, "close": 50_000.0, "bb_pct_b": 0.8,
    "momentum_20": 0.05, "volume_zscore": 2.0, "vol_percentile": 0.5,
}


def test_good_signal_scores_high():
    score = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, micro={"trade_imbalance": 0.4, "depth_imbalance": 0.2},
        regime=Regime.STRONG_UPTREND, data_quality=1.0, event_evidence=0.3,
        liquidity_ok=True, spread_pct=0.02,
    ))
    assert score.final_score > 55
    assert score.market_regime_score == 100.0


def test_zero_data_quality_kills_score():
    score = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.STRONG_UPTREND, data_quality=0.0,
        liquidity_ok=True,
    ))
    assert score.final_score == 0.0


def test_panic_regime_kills_long_score():
    score = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.PANIC, data_quality=1.0, liquidity_ok=True,
    ))
    assert score.final_score == 0.0


def test_unknown_regime_kills_score():
    score = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.UNKNOWN, data_quality=1.0, liquidity_ok=True,
    ))
    assert score.final_score == 0.0


def test_illiquidity_drags_score():
    liquid = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.STRONG_UPTREND, data_quality=1.0,
        liquidity_ok=True, spread_pct=0.01,
    ))
    illiquid = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.STRONG_UPTREND, data_quality=1.0,
        liquidity_ok=False,
    ))
    assert illiquid.final_score < liquid.final_score
    assert illiquid.liquidity_score == 0.0


def test_contradictory_event_evidence_lowers_score():
    supportive = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.STRONG_UPTREND, data_quality=1.0,
        event_evidence=0.5, liquidity_ok=True,
    ))
    contradictory = fuse(_signal(), FusionInputs(
        features=BULL_FEATURES, regime=Regime.STRONG_UPTREND, data_quality=1.0,
        event_evidence=-0.8, liquidity_ok=True,
    ))
    assert contradictory.event_score < 50 < supportive.event_score
    assert contradictory.final_score < supportive.final_score
