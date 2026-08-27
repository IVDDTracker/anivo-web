"""Baseline research strategies (STRATEGY_RESEARCH.md).

Deliberately simple and interpretable. Every signal carries evidence + risks so
notifications can explain WHY it exists.
"""

from __future__ import annotations

from app.models.enums import Direction, Regime
from app.models.signals import EvidenceItem
from app.strategies.base import BaseStrategy, OpenPositionView, Signal, StrategyContext


class TrendMomentumStrategy(BaseStrategy):
    name = "trend_momentum"
    version = "1.0"
    required_features = ("ema50", "ema200", "ema_structure_bull", "momentum_20",
                         "vwap_dev_pct", "atr14", "rsi14")
    eligible_regimes = frozenset({Regime.STRONG_UPTREND, Regime.WEAK_UPTREND})
    risk_profile = {"style": "trend", "expected_hold_bars": 48}

    @classmethod
    def default_params(cls) -> dict:
        return {"min_momentum": 0.02, "atr_stop_mult": 2.0, "atr_target_mult": 3.5,
                "max_rsi": 80.0}

    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        if not self.has_required_features(ctx.features):
            return None
        f = ctx.features
        p = self.params
        if f["ema_structure_bull"] != 1.0:
            return None
        if f["momentum_20"] < p["min_momentum"]:
            return None
        if f["vwap_dev_pct"] <= 0:
            return None
        if f["rsi14"] >= p["max_rsi"]:
            return None
        atr = f["atr14"]
        stop = ctx.close - p["atr_stop_mult"] * atr
        target = ctx.close + p["atr_target_mult"] * atr
        confidence = min(90.0, 55.0 + f["momentum_20"] * 300.0 + max(0.0, f["vwap_dev_pct"]) * 2)
        evidence = [
            EvidenceItem(name="price above trend structure (close>EMA50>EMA200)", value=True),
            EvidenceItem(name="20-bar momentum", value=round(f["momentum_20"], 4)),
            EvidenceItem(name="price above rolling VWAP (%)", value=round(f["vwap_dev_pct"], 3)),
        ]
        risks = []
        if f["rsi14"] > 70:
            risks.append(f"RSI elevated ({f['rsi14']:.0f}) — extended move")
        if ctx.features.get("vol_percentile", 0.5) > 0.85:
            risks.append("volatility elevated vs own history")
        return self._base_signal(ctx, Direction.LONG, confidence=confidence, stop=stop,
                                 target=target, evidence=evidence, risks=risks,
                                 expected_holding_h=48.0)

    def should_exit(self, ctx: StrategyContext, position: OpenPositionView) -> str | None:
        f = ctx.features
        if f and f.get("ema50") and ctx.close < f["ema50"]:
            return "trend structure broken (close < EMA50)"
        return None


class VolumeBreakoutStrategy(BaseStrategy):
    name = "volume_breakout"
    version = "1.0"
    required_features = ("donchian_upper", "donchian_breakout_up", "volume_zscore",
                         "range_expansion", "atr14")
    eligible_regimes = frozenset({Regime.STRONG_UPTREND, Regime.WEAK_UPTREND, Regime.RANGE,
                                  Regime.VOL_COMPRESSION, Regime.VOL_EXPANSION})
    risk_profile = {"style": "breakout", "expected_hold_bars": 24}

    @classmethod
    def default_params(cls) -> dict:
        return {"min_volume_z": 2.0, "min_range_expansion": 1.2, "atr_stop_mult": 1.5,
                "atr_target_mult": 2.5}

    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        if not self.has_required_features(ctx.features):
            return None
        f = ctx.features
        p = self.params
        if f["donchian_breakout_up"] != 1.0:
            return None
        if f["volume_zscore"] < p["min_volume_z"]:
            return None
        if f["range_expansion"] < p["min_range_expansion"]:
            return None
        atr = f["atr14"]
        breakout_level = f["donchian_upper"]
        stop = max(breakout_level - 0.25 * atr, ctx.close - p["atr_stop_mult"] * atr)
        target = ctx.close + p["atr_target_mult"] * atr
        confidence = min(90.0, 50.0 + f["volume_zscore"] * 8.0 + (f["range_expansion"] - 1) * 10)
        evidence = [
            EvidenceItem(name="Donchian(55) breakout confirmed", value=True),
            EvidenceItem(name="volume z-score", value=round(f["volume_zscore"], 2)),
            EvidenceItem(name="range expansion", value=round(f["range_expansion"], 2)),
        ]
        risks = ["breakouts fail frequently in chop; invalidation at breakout level"]
        return self._base_signal(ctx, Direction.LONG, confidence=confidence, stop=stop,
                                 target=target, evidence=evidence, risks=risks,
                                 invalidation=breakout_level, expected_holding_h=24.0)


class RangeMeanReversionStrategy(BaseStrategy):
    name = "range_mean_reversion"
    version = "1.0"
    required_features = ("bb_pct_b", "rsi14", "atr14", "ema20")
    eligible_regimes = frozenset({Regime.RANGE, Regime.VOL_COMPRESSION})
    risk_profile = {"style": "mean_reversion", "expected_hold_bars": 12}

    @classmethod
    def default_params(cls) -> dict:
        return {"max_pct_b": 0.05, "max_rsi": 32.0, "atr_stop_mult": 2.0}

    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        if not self.has_required_features(ctx.features):
            return None
        f = ctx.features
        p = self.params
        if f["bb_pct_b"] > p["max_pct_b"]:
            return None
        if f["rsi14"] > p["max_rsi"]:
            return None
        # require the bar to close up (recovering), not knife-catching
        if ctx.candles[-1].close <= ctx.candles[-1].open:
            return None
        atr = f["atr14"]
        stop = ctx.close - p["atr_stop_mult"] * atr
        target = f["ema20"]  # mean reversion targets the middle of the range
        if target <= ctx.close:
            return None
        confidence = min(85.0, 50.0 + (p["max_rsi"] - f["rsi14"]) * 1.5)
        evidence = [
            EvidenceItem(name="Bollinger %B", value=round(f["bb_pct_b"], 3)),
            EvidenceItem(name="RSI oversold, recovering bar", value=round(f["rsi14"], 1)),
            EvidenceItem(name="target = EMA20 (range mid)", value=round(target, 2)),
        ]
        risks = ["mean reversion loses in trends; regime filter is load-bearing"]
        return self._base_signal(ctx, Direction.LONG, confidence=confidence, stop=stop,
                                 target=target, evidence=evidence, risks=risks,
                                 expected_holding_h=12.0)

    def should_exit(self, ctx: StrategyContext, position: OpenPositionView) -> str | None:
        if ctx.features and ctx.features.get("bb_pct_b", 0.0) >= 0.55:
            return "reverted to range mid (%B >= 0.55)"
        return None


BASELINE_STRATEGIES: dict[str, type[BaseStrategy]] = {
    TrendMomentumStrategy.name: TrendMomentumStrategy,
    VolumeBreakoutStrategy.name: VolumeBreakoutStrategy,
    RangeMeanReversionStrategy.name: RangeMeanReversionStrategy,
}
