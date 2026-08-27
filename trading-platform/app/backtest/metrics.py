"""Backtest performance metrics (see task list in README/requirements)."""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from app.backtest.engine import BacktestResult, BtTrade

_BARS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040, "1h": 8_760, "4h": 2_190, "1d": 365}


def compute_metrics(result: BacktestResult, *, initial_equity: float) -> dict:
    curve = result.equity_curve
    trades = [t for t in result.trades if t.exit_time is not None]
    metrics: dict = {
        "n_trades": len(trades),
        "signals_generated": result.signals_generated,
        "signals_skipped_regime": result.signals_skipped_regime,
    }
    if not curve:
        metrics["total_return_pct"] = 0.0
        return metrics

    equity = np.array([e for _, e in curve])
    times = [t for t, _ in curve]
    final = equity[-1]
    metrics["final_equity"] = round(float(final), 2)
    metrics["total_return_pct"] = round((final / initial_equity - 1.0) * 100.0, 3)

    bars_per_year = _BARS_PER_YEAR.get(result.timeframe, 8_760)
    n_bars = len(equity)
    years = n_bars / bars_per_year
    if years > 0.05 and final > 0:
        metrics["annualized_return_pct"] = round(
            ((final / initial_equity) ** (1 / years) - 1.0) * 100.0, 2)

    # drawdown
    running_max = np.maximum.accumulate(equity)
    dd = (equity - running_max) / running_max
    max_dd = float(-dd.min()) if len(dd) else 0.0
    metrics["max_drawdown_pct"] = round(max_dd * 100.0, 3)

    # per-bar returns → sharpe/sortino/VaR
    rets = np.diff(equity) / equity[:-1]
    if len(rets) > 10 and rets.std(ddof=0) > 0:
        ann = math.sqrt(bars_per_year)
        metrics["sharpe"] = round(float(rets.mean() / rets.std(ddof=0) * ann), 3)
        downside = rets[rets < 0]
        if len(downside) > 2 and downside.std(ddof=0) > 0:
            metrics["sortino"] = round(float(rets.mean() / downside.std(ddof=0) * ann), 3)
        metrics["var95_pct"] = round(float(-np.percentile(rets, 5)) * 100.0, 4)
        tail = rets[rets <= np.percentile(rets, 5)]
        if len(tail):
            metrics["cvar95_pct"] = round(float(-tail.mean()) * 100.0, 4)
    ann_ret = metrics.get("annualized_return_pct")
    if ann_ret is not None and max_dd > 0:
        metrics["calmar"] = round(ann_ret / (max_dd * 100.0), 3)

    # exposure: fraction of bars with an open position
    in_market_bars = 0
    intervals = [(t.entry_time, t.exit_time) for t in trades]
    for ts in times:
        if any(a <= ts <= b for a, b in intervals):
            in_market_bars += 1
    metrics["exposure_pct"] = round(in_market_bars / n_bars * 100.0, 2)

    if trades:
        pnls = np.array([t.pnl for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        metrics["win_rate_pct"] = round(len(wins) / len(pnls) * 100.0, 2)
        metrics["avg_win"] = round(float(wins.mean()), 4) if len(wins) else 0.0
        metrics["avg_loss"] = round(float(losses.mean()), 4) if len(losses) else 0.0
        if len(losses) and losses.mean() != 0:
            metrics["win_loss_ratio"] = round(
                abs(float(wins.mean()) / float(losses.mean())), 3) if len(wins) else 0.0
        gross_profit = float(wins.sum())
        gross_loss = abs(float(losses.sum()))
        metrics["profit_factor"] = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0)
        metrics["expectancy"] = round(float(pnls.mean()), 4)
        metrics["fees_paid"] = round(float(sum(t.fees for t in trades)), 4)
        metrics["slippage_paid"] = round(float(sum(t.slippage for t in trades)), 4)
        metrics["avg_holding_hours"] = round(
            float(np.mean([t.holding_hours for t in trades])), 2)
        metrics["turnover_notional"] = round(
            float(sum(t.qty * t.entry_price + t.qty * (t.exit_price or 0) for t in trades)), 2)
        net = float(pnls.sum())
        if net > 0:
            metrics["largest_winner_share"] = round(float(pnls.max()) / net, 3) if pnls.max() > 0 else 0.0

        # longest losing streak
        streak = longest = 0
        for p in pnls:
            streak = streak + 1 if p <= 0 else 0
            longest = max(longest, streak)
        metrics["longest_losing_streak"] = int(longest)

        # daily PnL distribution
        daily: dict = defaultdict(float)
        for t in trades:
            daily[t.exit_time.date()] += t.pnl
        dvals = np.array(list(daily.values()))
        if len(dvals) >= 5:
            metrics["daily_pnl_p5"] = round(float(np.percentile(dvals, 5)), 2)
            metrics["daily_pnl_median"] = round(float(np.median(dvals)), 2)
            metrics["daily_pnl_p95"] = round(float(np.percentile(dvals, 95)), 2)

    return metrics


def bootstrap_drawdown_ci(trades: list[BtTrade], initial_equity: float, *, n_boot: int = 500,
                          seed: int = 1) -> dict:
    """Bootstrap trade order to estimate drawdown distribution (order dependence check)."""
    pnls = np.array([t.pnl for t in trades if t.exit_time is not None])
    if len(pnls) < 10:
        return {}
    rng = np.random.default_rng(seed)
    dds = []
    for _ in range(n_boot):
        sample = rng.permutation(pnls)
        eq = initial_equity + np.cumsum(sample)
        peak = np.maximum.accumulate(np.insert(eq, 0, initial_equity))
        dd = ((np.insert(eq, 0, initial_equity) - peak) / peak).min()
        dds.append(-dd * 100.0)
    return {
        "bootstrap_dd_median_pct": round(float(np.median(dds)), 2),
        "bootstrap_dd_p95_pct": round(float(np.percentile(dds, 95)), 2),
    }
