"""Interpretable market regime classifier (no ML by design — DECISIONS.md D-015).

Inputs are the feature vector of the regime timeframe (default 1h) plus candle
history. Every classification returns the rule trail that produced it, so a
regime change is always explainable.

Priority order (first match wins):
  PANIC > LOW_LIQUIDITY > trend states > volatility states > RANGE
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config.settings import RegimeConfig
from app.models.enums import Regime
from app.models.market import Candle


@dataclass
class RegimeResult:
    regime: Regime
    volatility_state: str  # "low" | "normal" | "high"
    rules_fired: list[str]
    metrics: dict[str, float]


def classify(
    candles: list[Candle],
    features: dict[str, float],
    cfg: RegimeConfig,
    *,
    quote_volume_24h: float | None = None,
    min_liquidity: float = 0.0,
) -> RegimeResult:
    rules: list[str] = []
    metrics: dict[str, float] = {}
    if len(candles) < cfg.ema_fast + 10 or not features:
        return RegimeResult(Regime.UNKNOWN, "normal", ["insufficient history"], metrics)

    closes = np.array([c.close for c in candles])
    close = closes[-1]

    # volatility state from realized-vol percentile of own history
    vol_pct = features.get("vol_percentile", float("nan"))
    metrics["vol_percentile"] = vol_pct
    if not np.isnan(vol_pct) and vol_pct >= cfg.high_vol_percentile:
        vol_state = "high"
    elif not np.isnan(vol_pct) and vol_pct <= cfg.low_vol_percentile:
        vol_state = "low"
    else:
        vol_state = "normal"
    rules.append(f"vol_percentile={vol_pct:.2f}→{vol_state}")

    # PANIC: fast deep drawdown within a short window
    window = min(cfg.panic_window_bars, len(closes))
    peak = closes[-window:].max()
    drawdown_pct = (peak - close) / peak * 100.0 if peak > 0 else 0.0
    metrics["fast_drawdown_pct"] = drawdown_pct
    if drawdown_pct >= cfg.panic_drawdown_pct:
        rules.append(f"drawdown {drawdown_pct:.1f}% ≥ {cfg.panic_drawdown_pct}% in {window} bars → PANIC")
        return RegimeResult(Regime.PANIC, vol_state, rules, metrics)

    # LOW_LIQUIDITY: 24h quote volume below the configured floor
    if quote_volume_24h is not None and min_liquidity > 0 and quote_volume_24h < min_liquidity:
        rules.append(f"24h quote volume {quote_volume_24h:.0f} < {min_liquidity:.0f} → LOW_LIQUIDITY")
        return RegimeResult(Regime.LOW_LIQUIDITY, vol_state, rules, metrics)

    ema50 = features.get("ema50", float("nan"))
    ema200 = features.get("ema200", float("nan"))
    slope = features.get("ema50_slope_pct", float("nan"))
    metrics["ema50_slope_pct"] = slope
    has_long_ema = not np.isnan(ema200)

    # trend states
    if has_long_ema and close > ema50 > ema200:
        if not np.isnan(slope) and slope > 0.5 and features.get("momentum_20", 0) > 0.02:
            rules.append("close>ema50>ema200, strong slope & momentum → STRONG_UPTREND")
            return RegimeResult(Regime.STRONG_UPTREND, vol_state, rules, metrics)
        rules.append("close>ema50>ema200 → WEAK_UPTREND")
        return RegimeResult(Regime.WEAK_UPTREND, vol_state, rules, metrics)
    if has_long_ema and close < ema50 < ema200:
        rules.append("close<ema50<ema200 → DOWNTREND")
        return RegimeResult(Regime.DOWNTREND, vol_state, rules, metrics)

    # volatility transition states (only when trendless)
    vol_accel = features.get("vol_acceleration", 0.0)
    metrics["vol_acceleration"] = vol_accel
    if vol_state == "low" and abs(vol_accel) < 0.1:
        rules.append("low vol percentile, flat vol → VOL_COMPRESSION")
        return RegimeResult(Regime.VOL_COMPRESSION, vol_state, rules, metrics)
    if vol_accel > 0.5:
        rules.append(f"vol acceleration {vol_accel:.2f} → VOL_EXPANSION")
        return RegimeResult(Regime.VOL_EXPANSION, vol_state, rules, metrics)
    if vol_state == "high":
        rules.append("trendless with high vol → HIGH_VOL_RANGE")
        return RegimeResult(Regime.HIGH_VOL_RANGE, vol_state, rules, metrics)

    rules.append("no trend structure, normal vol → RANGE")
    return RegimeResult(Regime.RANGE, vol_state, rules, metrics)
