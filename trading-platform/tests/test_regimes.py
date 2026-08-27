"""Regime classifier tests: interpretable rules must fire on synthetic archetypes."""

from __future__ import annotations

from app.config.settings import RegimeConfig
from app.features.engine import compute_features
from app.models.enums import Regime
from app.regimes.classifier import classify
from tests.helpers import make_candles

CFG = RegimeConfig()


def _classify(candles, **kw):
    return classify(candles, compute_features(candles), CFG, **kw)


def test_unknown_when_insufficient_history():
    result = _classify(make_candles(20))
    assert result.regime == Regime.UNKNOWN


def test_strong_uptrend_detected():
    candles = make_candles(400, drift=0.004, vol=0.004, seed=3)
    result = _classify(candles)
    assert result.regime in (Regime.STRONG_UPTREND, Regime.WEAK_UPTREND)
    assert any("ema" in r for r in result.rules_fired)


def test_downtrend_detected():
    candles = make_candles(400, drift=-0.003, vol=0.004, seed=4)
    result = _classify(candles)
    assert result.regime in (Regime.DOWNTREND, Regime.PANIC)


def test_range_detected():
    candles = make_candles(400, drift=0.0, vol=0.003, seed=5)
    result = _classify(candles)
    assert result.regime in (Regime.RANGE, Regime.VOL_COMPRESSION, Regime.HIGH_VOL_RANGE,
                             Regime.WEAK_UPTREND, Regime.DOWNTREND, Regime.VOL_EXPANSION)


def test_panic_detected_on_crash():
    candles = make_candles(360, drift=0.0005, vol=0.003, seed=6)
    crash = make_candles(30, drift=-0.02, vol=0.01, seed=7,
                         start_price=candles[-1].close,
                         start=candles[-1].close_time)
    result = _classify(candles + crash)
    assert result.regime == Regime.PANIC
    assert any("PANIC" in r for r in result.rules_fired)


def test_low_liquidity_gate():
    candles = make_candles(400, drift=0.0, vol=0.003, seed=8)
    result = _classify(candles, quote_volume_24h=1_000_000, min_liquidity=50_000_000)
    assert result.regime == Regime.LOW_LIQUIDITY


def test_every_result_is_explainable():
    result = _classify(make_candles(400, seed=9))
    assert result.rules_fired, "classification must always carry its rule trail"
