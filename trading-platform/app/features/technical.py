"""Technical indicator primitives. Pure numpy, fully deterministic, no lookahead:
every function returns values aligned so index i uses ONLY bars [0..i].

Indicator set is intentionally small — each feature exists because a baseline
strategy or the regime classifier consumes it (see STRATEGY_RESEARCH.md).
"""

from __future__ import annotations

import numpy as np


def ema(values: np.ndarray, period: int) -> np.ndarray:
    if period <= 0:
        raise ValueError("period must be > 0")
    out = np.full(len(values), np.nan)
    if len(values) == 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    out = np.full(len(closes), np.nan)
    if len(closes) <= period:
        return out
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rs = avg_gain / avg_loss if avg_loss > 1e-12 else np.inf
    out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 1e-12 else np.inf
        out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(closes, fast) - ema(closes, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    prev_close = close[:-1]
    tr[1:] = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev_close),
        np.abs(low[1:] - prev_close),
    ])
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder-smoothed ATR."""
    tr = true_range(high, low, close)
    out = np.full(len(close), np.nan)
    if len(close) < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger(closes: np.ndarray, period: int = 20, num_std: float = 2.0
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (mid, upper, lower, percent_b)."""
    mid = sma(closes, period)
    std = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        std[i] = closes[i - period + 1: i + 1].std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = upper - lower
    with np.errstate(invalid="ignore", divide="ignore"):
        pct_b = np.where(width > 0, (closes - lower) / width, 0.5)
    return mid, upper, lower, pct_b


def donchian(high: np.ndarray, low: np.ndarray, period: int = 55
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (upper, lower, position 0..1). Upper/lower EXCLUDE the current bar
    (previous N bars) so a breakout of the channel is detectable without lookahead."""
    n = len(high)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period, n):
        upper[i] = high[i - period: i].max()
        lower[i] = low[i - period: i].min()
    width = upper - lower
    close_mid = (high + low) / 2.0
    with np.errstate(invalid="ignore", divide="ignore"):
        pos = np.where(width > 0, (close_mid - lower) / width, 0.5)
    return upper, lower, pos


def rolling_vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 volume: np.ndarray, period: int = 24) -> np.ndarray:
    typical = (high + low + close) / 3.0
    out = np.full(len(close), np.nan)
    for i in range(period - 1, len(close)):
        v = volume[i - period + 1: i + 1]
        tp = typical[i - period + 1: i + 1]
        vol_sum = v.sum()
        out[i] = (tp * v).sum() / vol_sum if vol_sum > 0 else np.nan
    return out


def log_returns(closes: np.ndarray) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    out[1:] = np.diff(np.log(closes))
    return out


def realized_vol(closes: np.ndarray, period: int = 30) -> np.ndarray:
    """Rolling std of log returns (per-bar, NOT annualized — comparably scaled per timeframe)."""
    rets = log_returns(closes)
    out = np.full(len(closes), np.nan)
    for i in range(period, len(closes)):
        out[i] = np.nanstd(rets[i - period + 1: i + 1], ddof=0)
    return out


def momentum(closes: np.ndarray, period: int = 20) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    out[period:] = closes[period:] / closes[:-period] - 1.0
    return out


def zscore_last(values: np.ndarray, period: int = 20) -> float:
    """z-score of the last value vs the PRIOR `period` values (excludes itself)."""
    if len(values) < period + 1:
        return float("nan")
    window = values[-period - 1: -1]
    mu, sd = window.mean(), window.std(ddof=0)
    return float((values[-1] - mu) / sd) if sd > 1e-12 else 0.0


def percentile_rank_last(values: np.ndarray, lookback: int) -> float:
    """Percentile (0..1) of the last value within its own trailing history."""
    window = values[-lookback:] if len(values) >= lookback else values
    window = window[~np.isnan(window)]
    if len(window) < 10:
        return float("nan")
    return float((window <= window[-1]).mean())
