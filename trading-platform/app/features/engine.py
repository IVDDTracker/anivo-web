"""Feature engine: candle history → feature vector per (symbol, timeframe).

The engine only ever computes features from CLOSED bars it has already received;
`compute(history)` is a pure function used identically by live pipeline, replay
and backtests, which eliminates live-vs-backtest feature drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.features import technical as ta
from app.models.market import Candle

MAX_HISTORY = 1200
MIN_BARS = 60


def compute_features(candles: list[Candle]) -> dict[str, float]:
    """Pure feature computation over closed candles (last bar = most recent closed)."""
    if len(candles) < MIN_BARS:
        return {}
    closes = np.array([c.close for c in candles])
    highs = np.array([c.high for c in candles])
    lows = np.array([c.low for c in candles])
    volumes = np.array([c.volume for c in candles])
    taker_buy = np.array([c.taker_buy_base for c in candles])

    out: dict[str, float] = {"close": float(closes[-1])}

    ema20 = ta.ema(closes, 20)
    ema50 = ta.ema(closes, 50)
    ema200 = ta.ema(closes, 200) if len(closes) >= 200 else np.full(len(closes), np.nan)
    out["ema20"] = float(ema20[-1])
    out["ema50"] = float(ema50[-1])
    out["ema200"] = float(ema200[-1])
    out["ema50_slope_pct"] = (
        float((ema50[-1] / ema50[-6] - 1.0) * 100.0) if not np.isnan(ema50[-6:]).any() else float("nan")
    )
    out["above_ema50"] = float(closes[-1] > ema50[-1])
    out["ema_structure_bull"] = float(
        closes[-1] > ema50[-1] > ema200[-1]) if not np.isnan(ema200[-1]) else float("nan")

    rsi14 = ta.rsi(closes, 14)
    out["rsi14"] = float(rsi14[-1])
    macd_line, macd_sig, macd_hist = ta.macd(closes)
    out["macd_hist"] = float(macd_hist[-1])

    atr14 = ta.atr(highs, lows, closes, 14)
    out["atr14"] = float(atr14[-1])
    out["atr_pct"] = float(atr14[-1] / closes[-1] * 100.0) if closes[-1] > 0 else float("nan")

    _, upper, lower, pct_b = ta.bollinger(closes, 20, 2.0)
    out["bb_pct_b"] = float(pct_b[-1])
    out["bb_width_pct"] = (
        float((upper[-1] - lower[-1]) / closes[-1] * 100.0) if closes[-1] > 0 else float("nan")
    )

    don_u, don_l, don_pos = ta.donchian(highs, lows, 55)
    out["donchian_upper"] = float(don_u[-1])
    out["donchian_lower"] = float(don_l[-1])
    out["donchian_pos"] = float(don_pos[-1])
    out["donchian_breakout_up"] = float(closes[-1] > don_u[-1]) if not np.isnan(don_u[-1]) else 0.0
    out["donchian_breakout_down"] = float(closes[-1] < don_l[-1]) if not np.isnan(don_l[-1]) else 0.0

    vwap24 = ta.rolling_vwap(highs, lows, closes, volumes, 24)
    out["vwap_dev_pct"] = (
        float((closes[-1] / vwap24[-1] - 1.0) * 100.0) if vwap24[-1] and not np.isnan(vwap24[-1]) else float("nan")
    )

    rv30 = ta.realized_vol(closes, 30)
    out["realized_vol"] = float(rv30[-1])
    prior = rv30[-11:-1]
    if not np.isnan(prior).all():
        base = np.nanmean(prior)
        out["vol_acceleration"] = float(rv30[-1] / base - 1.0) if base > 0 else 0.0
    out["vol_percentile"] = ta.percentile_rank_last(rv30, 365)

    out["momentum_20"] = float(ta.momentum(closes, 20)[-1])
    out["momentum_5"] = float(ta.momentum(closes, 5)[-1])
    out["return_1"] = float(closes[-1] / closes[-2] - 1.0)

    out["volume_zscore"] = ta.zscore_last(volumes, 20)
    total_recent = volumes[-20:].sum()
    out["taker_buy_ratio"] = float(taker_buy[-20:].sum() / total_recent) if total_recent > 0 else 0.5

    # range expansion: current true range vs its 20-bar average
    tr = ta.true_range(highs, lows, closes)
    tr_avg = tr[-21:-1].mean()
    out["range_expansion"] = float(tr[-1] / tr_avg) if tr_avg > 0 else float("nan")

    return out


@dataclass
class FeatureEngine:
    max_history: int = MAX_HISTORY
    _history: dict[tuple[str, str], list[Candle]] = field(default_factory=dict)
    _latest: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)

    def warm(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        history = sorted(candles, key=lambda c: c.open_time)[-self.max_history:]
        self._history[(symbol, timeframe)] = history

    def on_candle(self, candle: Candle) -> dict[str, float]:
        key = (candle.symbol, candle.timeframe)
        history = self._history.setdefault(key, [])
        if history and history[-1].open_time == candle.open_time:
            history[-1] = candle  # replacement (e.g. backfill correction)
        elif history and candle.open_time < history[-1].open_time:
            return self._latest.get(key, {})  # late out-of-order bar: ignore for features
        else:
            history.append(candle)
        if len(history) > self.max_history:
            del history[: len(history) - self.max_history]
        feats = compute_features(history)
        if feats:
            self._latest[key] = feats
        return feats

    def latest(self, symbol: str, timeframe: str) -> dict[str, float]:
        return self._latest.get((symbol, timeframe), {})

    def history(self, symbol: str, timeframe: str) -> list[Candle]:
        return self._history.get((symbol, timeframe), [])
