"""Strategy scorecard (0-100). Weights documented in STRATEGY_RESEARCH.md:

OOS risk-adjusted return 30 · drawdown 15 · sample size 10 · parameter
stability 15 · regime robustness 10 · asset robustness 10 · cost sensitivity 5 ·
recent degradation 5.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScorecardInputs:
    oos_profit_factor: float = 0.0
    oos_expectancy: float = 0.0
    oos_trades: int = 0
    profitable_windows: int = 0
    total_windows: int = 0
    max_drawdown_pct: float = 100.0
    sharpe: float = 0.0
    parameter_stable: bool = False
    min_perturbed_pf: float | None = None
    profitable_assets: int = 0
    tested_assets: int = 0
    fee_sensitivity_ratio: float = 0.0   # pf(2x fees) / pf(base), 0..1+
    recent_degradation: bool = False


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_scorecard(inputs: ScorecardInputs) -> dict:
    parts: dict[str, float] = {}

    # OOS risk-adjusted performance (30)
    pf = inputs.oos_profit_factor
    pf_score = _clamp01((pf - 1.0) / 1.0) if pf not in (float("inf"),) else 1.0
    sharpe_score = _clamp01(inputs.sharpe / 2.0)
    window_score = (inputs.profitable_windows / inputs.total_windows
                    if inputs.total_windows else 0.0)
    parts["oos_performance"] = 30.0 * (0.5 * pf_score + 0.25 * sharpe_score
                                       + 0.25 * window_score)

    # drawdown (15): 0% dd → full, 25%+ → zero
    parts["drawdown"] = 15.0 * _clamp01(1.0 - inputs.max_drawdown_pct / 25.0)

    # sample size (10): full credit at 100 OOS trades
    parts["sample_size"] = 10.0 * _clamp01(inputs.oos_trades / 100.0)

    # parameter stability (15)
    if inputs.min_perturbed_pf is None:
        parts["parameter_stability"] = 0.0
    else:
        parts["parameter_stability"] = 15.0 * _clamp01(inputs.min_perturbed_pf / 1.2)

    # regime robustness (10): proxied by profitable window share until regime-split
    # backtests land (documented simplification)
    parts["regime_robustness"] = 10.0 * window_score

    # asset robustness (10)
    parts["asset_robustness"] = 10.0 * (inputs.profitable_assets / inputs.tested_assets
                                        if inputs.tested_assets else 0.0)

    # transaction cost sensitivity (5): pf must survive doubled fees
    parts["cost_sensitivity"] = 5.0 * _clamp01(inputs.fee_sensitivity_ratio)

    # recent degradation (5)
    parts["recent_degradation"] = 0.0 if inputs.recent_degradation else 5.0

    total = round(sum(parts.values()), 1)
    return {"total": total, "parts": {k: round(v, 2) for k, v in parts.items()}}
