"""Signal fusion: independent evidence components → meta score.

Weights are explicit research priors (DECISIONS.md D-014): additive evidence
components are equal-weighted within the direction-adjusted evidence set, while
regime alignment and data quality act as MULTIPLICATIVE gates — bad data or a
hostile regime cannot be "averaged away" by pretty technicals.
`app/research/fusion_weights.py` is the only sanctioned way to change weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.models.enums import Direction, Regime
from app.models.signals import FusionScore, Signal

# regime alignment per direction: 1.0 = fully aligned, 0 = forbidden
_REGIME_ALIGNMENT_LONG: dict[Regime, float] = {
    Regime.STRONG_UPTREND: 1.0,
    Regime.WEAK_UPTREND: 0.85,
    Regime.RANGE: 0.6,
    Regime.VOL_COMPRESSION: 0.6,
    Regime.VOL_EXPANSION: 0.5,
    Regime.HIGH_VOL_RANGE: 0.35,
    Regime.DOWNTREND: 0.2,
    Regime.LOW_LIQUIDITY: 0.0,
    Regime.PANIC: 0.0,
    Regime.UNKNOWN: 0.0,
}

WEIGHTS_VERSION = "prior-v1"

_COMPONENT_WEIGHTS: dict[str, float] = {
    # equal priors within evidence set (sum normalized at runtime)
    "technical_score": 1.0,
    "momentum_score": 1.0,
    "microstructure_score": 1.0,
    "volume_score": 1.0,
    "volatility_score": 1.0,
    "event_score": 1.0,
    "sentiment_score": 0.5,   # sentiment is weak evidence by design
    "cross_asset_score": 0.5,
    "liquidity_score": 1.0,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _sigmoid_score(x: float, scale: float) -> float:
    """Map a signed feature to 0..100 with 50 = neutral."""
    if math.isnan(x):
        return 50.0
    return _clamp(100.0 / (1.0 + math.exp(-x / scale)))


@dataclass
class FusionInputs:
    features: dict[str, float] = field(default_factory=dict)
    micro: dict[str, float] = field(default_factory=dict)
    regime: Regime = Regime.UNKNOWN
    data_quality: float = 0.0
    event_evidence: float = 0.0      # -1..1 decayed, confirmation-weighted external evidence
    sentiment: float = 0.0           # -1..1
    cross_asset_momentum: float = 0.0  # e.g. BTC momentum when scoring alts
    liquidity_ok: bool = False
    spread_pct: float = float("nan")


def fuse(signal: Signal, inputs: FusionInputs) -> FusionScore:
    f = inputs.features
    direction = 1.0 if signal.direction == Direction.LONG else -1.0

    technical = _sigmoid_score(
        direction * (
            (1.0 if f.get("ema_structure_bull") == 1.0 else -0.5)
            + (f.get("macd_hist", 0.0) / max(abs(f.get("close", 1.0)) * 1e-3, 1e-9)) * 0.2
            + (f.get("bb_pct_b", 0.5) - 0.5)
        ),
        scale=1.0,
    )
    momentum_score = _sigmoid_score(direction * f.get("momentum_20", 0.0) * 100.0, scale=3.0)
    micro_imb = inputs.micro.get("trade_imbalance", 0.0) + inputs.micro.get("depth_imbalance", 0.0)
    microstructure = _sigmoid_score(direction * micro_imb, scale=0.4)
    volume_score = _sigmoid_score(f.get("volume_zscore", 0.0), scale=1.5)
    # volatility: prefer normal vol; extremes reduce score regardless of direction
    vol_pct = f.get("vol_percentile", float("nan"))
    volatility = 50.0 if math.isnan(vol_pct) else _clamp(100.0 - abs(vol_pct - 0.5) * 120.0)
    event_score = _clamp(50.0 + direction * inputs.event_evidence * 50.0)
    sentiment_score = _clamp(50.0 + direction * inputs.sentiment * 25.0)
    cross_asset = _sigmoid_score(direction * inputs.cross_asset_momentum * 100.0, scale=5.0)
    if inputs.liquidity_ok and not math.isnan(inputs.spread_pct):
        liquidity = _clamp(100.0 - inputs.spread_pct * 800.0)
    elif inputs.liquidity_ok:
        liquidity = 60.0
    else:
        liquidity = 0.0

    components = {
        "technical_score": technical,
        "momentum_score": momentum_score,
        "microstructure_score": microstructure,
        "volume_score": volume_score,
        "volatility_score": volatility,
        "event_score": event_score,
        "sentiment_score": sentiment_score,
        "cross_asset_score": cross_asset,
        "liquidity_score": liquidity,
    }
    total_weight = sum(_COMPONENT_WEIGHTS.values())
    evidence = sum(components[k] * w for k, w in _COMPONENT_WEIGHTS.items()) / total_weight

    alignment = _REGIME_ALIGNMENT_LONG.get(inputs.regime, 0.0)
    if signal.direction == Direction.SHORT:
        # invert trend-alignment for shorts; panic/low-liquidity stay forbidden
        alignment = 0.0 if alignment == 0.0 and inputs.regime in (
            Regime.PANIC, Regime.LOW_LIQUIDITY, Regime.UNKNOWN
        ) else max(0.0, 1.0 - alignment)

    regime_score = alignment * 100.0
    final = evidence * alignment * max(0.0, min(1.0, inputs.data_quality))

    return FusionScore(
        technical_score=round(technical, 2),
        momentum_score=round(momentum_score, 2),
        microstructure_score=round(microstructure, 2),
        volume_score=round(volume_score, 2),
        volatility_score=round(volatility, 2),
        market_regime_score=round(regime_score, 2),
        event_score=round(event_score, 2),
        sentiment_score=round(sentiment_score, 2),
        cross_asset_score=round(cross_asset, 2),
        liquidity_score=round(liquidity, 2),
        final_score=round(final, 2),
        weights_version=WEIGHTS_VERSION,
    )
