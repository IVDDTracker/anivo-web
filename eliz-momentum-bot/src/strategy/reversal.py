"""Reversal score 0-100 (spec §8): market-behavior based, never a fixed timer.

Each component maps a microstructure observation to [0,1]; the weighted,
normalized sum is the score. Weights AND thresholds come from config so the
event study / backtest can optimize them — nothing here is hard-coded.

Gate: before the move has produced a real peak (peak gain < min_peak_gain_pct)
the score is 0 — a flat chop is not a "reversal".
"""

from __future__ import annotations

from pydantic import BaseModel

from src.core.config import ReversalParams, ReversalWeights
from src.strategy.momentum import MomentumMetrics


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class ReversalReading(BaseModel):
    score: float                       # 0-100
    components: dict[str, float]       # each 0-1 (pre-weight)
    gated: bool                        # True → score forced to 0 (no peak yet)


class ReversalScorer:
    def __init__(self, weights: ReversalWeights, params: ReversalParams) -> None:
        self.weights = weights
        self.params = params

    def score(self, m: MomentumMetrics) -> ReversalReading:
        p = self.params
        components = {
            # retrace off the peak
            "pullback_from_peak": _clamp01(m.drawdown_from_peak_pct / p.pullback_full_score_pct),
            # failure to make new highs
            "stale_high": _clamp01(m.seconds_since_new_high / p.stale_high_full_score_s),
            # trade velocity fading: ratio 1.0 → 0; ratio <= threshold → 1
            "velocity_drop": _clamp01((1.0 - m.velocity_ratio) / (1.0 - p.velocity_drop_ratio))
            if p.velocity_drop_ratio < 1.0 else 0.0,
            # aggressive flow flipping to sellers: buy_share 0.5 → 0; 0.25 → 1
            "flow_reversal": _clamp01((0.5 - m.buy_share) / 0.25),
            # order book tilting to the ask side
            "imbalance_shift": _clamp01(-m.depth_imbalance / 0.5),
            # short-term momentum turning negative
            "negative_momentum": _clamp01(-m.momentum_short_pct / 0.3),
            # price losing session VWAP
            "vwap_loss": _clamp01(-m.price_vs_vwap_pct / 0.3),
        }
        gated = m.peak_gain_pct < p.min_peak_gain_pct
        weight_map = self.weights.model_dump()
        total_weight = sum(weight_map.values()) or 1.0
        raw = sum(components[name] * weight_map.get(name, 0.0) for name in components)
        score = 0.0 if gated else round(raw / total_weight * 100.0, 2)
        return ReversalReading(score=score, components=components, gated=gated)
