"""Walk-forward validation, purged splits, and parameter-stability analysis.

The v1 baselines have fixed (non-fitted) parameters, so walk-forward here means:
evaluate on strictly out-of-sample rolling windows with a PURGE gap larger than
the maximum feature lookback, so no training-window information leaks across the
boundary through indicator state. Parameter stability perturbs each numeric
parameter by ±`perturbation_pct` and compares profit factors — the "RSI 29 vs 28/30"
red-flag detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.backtest.engine import BacktestConfig, Backtester
from app.backtest.metrics import compute_metrics
from app.models.market import Candle
from app.strategies.base import BaseStrategy

MAX_FEATURE_LOOKBACK = 200  # longest indicator lookback (EMA200)


@dataclass
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    metrics: dict = field(default_factory=dict)


@dataclass
class WalkForwardReport:
    strategy: str
    params: dict
    windows: list[WalkForwardWindow]
    oos_metrics: dict
    stability: dict


def make_windows(n_bars: int, *, train_bars: int, test_bars: int, step_bars: int,
                 purge_bars: int = MAX_FEATURE_LOOKBACK) -> list[WalkForwardWindow]:
    windows = []
    start = 0
    while start + train_bars + purge_bars + test_bars <= n_bars:
        windows.append(WalkForwardWindow(
            train_start=start,
            train_end=start + train_bars,
            test_start=start + train_bars + purge_bars,
            test_end=start + train_bars + purge_bars + test_bars,
        ))
        start += step_bars
    return windows


def run_walk_forward(
    strategy_cls: type[BaseStrategy],
    params: dict,
    candles: list[Candle],
    *,
    config: BacktestConfig | None = None,
    train_bars: int = 24 * 180,
    test_bars: int = 24 * 60,
    step_bars: int = 24 * 60,
) -> WalkForwardReport:
    cfg = config or BacktestConfig()
    windows = make_windows(len(candles), train_bars=train_bars, test_bars=test_bars,
                           step_bars=step_bars)
    all_oos_trades = []
    total_signals = 0
    for w in windows:
        # include warmup history before test window so features are warm at test start,
        # but only bars up to test_end are ever visible (no future leakage)
        segment_start = max(0, w.test_start - cfg.warmup_bars - MAX_FEATURE_LOOKBACK)
        segment = candles[segment_start: w.test_end]
        bt = Backtester(cfg)
        result = bt.run(strategy_cls(**params), segment)
        # only count trades entered inside the actual test window
        test_open = candles[w.test_start].open_time
        oos_trades = [t for t in result.trades if t.entry_time >= test_open]
        result.trades = oos_trades
        w.metrics = compute_metrics(result, initial_equity=cfg.initial_equity)
        all_oos_trades.extend(oos_trades)
        total_signals += result.signals_generated

    closed = [t for t in all_oos_trades if t.exit_time is not None]
    pnls = [t.pnl for t in closed]
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p <= 0))
    oos = {
        "oos_trades": len(closed),
        "oos_net_pnl": round(sum(pnls), 2),
        "oos_profit_factor": round(wins / losses, 3) if losses > 0 else (
            float("inf") if wins > 0 else 0.0),
        "oos_expectancy": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "oos_win_rate_pct": round(
            100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 2) if pnls else 0.0,
        "windows": len(windows),
        "profitable_windows": sum(
            1 for w in windows if w.metrics.get("total_return_pct", 0) > 0),
    }
    stability = parameter_stability(strategy_cls, params, candles, config=cfg)
    return WalkForwardReport(strategy=strategy_cls.name, params=params, windows=windows,
                             oos_metrics=oos, stability=stability)


def parameter_stability(
    strategy_cls: type[BaseStrategy],
    params: dict,
    candles: list[Candle],
    *,
    config: BacktestConfig | None = None,
    perturbation_pct: float = 20.0,
) -> dict:
    """Perturb each numeric param ±perturbation_pct; report profit-factor spread."""
    cfg = config or BacktestConfig()
    base = Backtester(cfg).run(strategy_cls(**params), candles)
    base_metrics = compute_metrics(base, initial_equity=cfg.initial_equity)
    base_pf = base_metrics.get("profit_factor", 0.0)
    variants: dict[str, float] = {}
    for key, value in params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for sign in (-1, 1):
            perturbed = dict(params)
            new_value = value * (1 + sign * perturbation_pct / 100.0)
            perturbed[key] = int(round(new_value)) if isinstance(value, int) else new_value
            result = Backtester(cfg).run(strategy_cls(**perturbed), candles)
            m = compute_metrics(result, initial_equity=cfg.initial_equity)
            variants[f"{key}{'+' if sign > 0 else '-'}{perturbation_pct:.0f}%"] = \
                m.get("profit_factor", 0.0)
    finite = [v for v in variants.values() if v not in (float("inf"),)]
    return {
        "base_profit_factor": base_pf,
        "perturbed": variants,
        "min_perturbed_pf": min(finite) if finite else None,
        "stable": bool(finite) and min(finite) >= 1.0 if finite else False,
    }
