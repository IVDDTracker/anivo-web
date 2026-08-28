"""Performance metrics report (spec §17): LONG leg vs SHORT leg split, because
the core research question is WHICH side of the hypothesis carries the edge.

    python -m src.metrics            # full report from the trades table
"""

from __future__ import annotations

import asyncio
import json

import numpy as np

from src.core.config import get_settings
from src.storage.database import Database, Repo


def leg_stats(pnls: list[float], fees: list[float], slippage: list[float]) -> dict:
    if not pnls:
        return {"trades": 0}
    arr = np.array(pnls)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gross_loss = abs(float(losses.sum()))
    equity = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))
    drawdown = float((peak[1:] - equity).max()) if len(equity) else 0.0
    return {
        "trades": len(arr),
        "win_rate_pct": round(len(wins) / len(arr) * 100, 1),
        "gross_pnl": round(float(arr.sum()) + sum(fees), 2),
        "net_pnl": round(float(arr.sum()), 2),
        "fees": round(sum(fees), 4),
        "slippage_cost": round(sum(slippage), 4),
        "profit_factor": round(float(wins.sum()) / gross_loss, 3) if gross_loss > 0
        else (float("inf") if len(wins) else 0.0),
        "expectancy": round(float(arr.mean()), 4),
        "avg_win": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "max_drawdown_usdt": round(drawdown, 2),
    }


async def build_report(repo: Repo) -> dict:
    trades = await repo.closed_trades()
    by_leg: dict[str, list] = {"LONG": [], "SHORT": []}
    for t in trades:
        by_leg.setdefault(t.leg, []).append(t)
    report: dict = {"total_trades": len(trades)}
    all_pnls, all_fees, all_slip = [], [], []
    for leg, rows in by_leg.items():
        pnls = [r.net_pnl for r in rows]
        fees = [r.fees for r in rows]
        slip = [r.slippage_cost for r in rows]
        report[f"{leg.lower()}_leg"] = leg_stats(pnls, fees, slip)
        all_pnls += pnls
        all_fees += fees
        all_slip += slip
    report["combined"] = leg_stats(all_pnls, all_fees, all_slip)
    return report


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_all()
    try:
        report = await build_report(Repo(db))
        print(json.dumps(report, indent=2, default=str))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
