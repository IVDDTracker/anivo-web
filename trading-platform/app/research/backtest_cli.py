"""Backtest CLI.

    python -m app.research.backtest_cli --strategy trend_momentum --symbol BTCUSDT \
        [--timeframe 1h] [--walk-forward] [--store]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.backtest.engine import BacktestConfig, Backtester
from app.backtest.metrics import compute_metrics
from app.backtest.walkforward import run_walk_forward
from app.config.settings import get_settings
from app.core.clock import utcnow
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, CandleRepository
from app.strategies.baselines import BASELINE_STRATEGIES


async def run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    db = Database(settings.database_url)
    try:
        candles = await CandleRepository(db).fetch(args.symbol, args.timeframe)
        if len(candles) < 300:
            return {"error": f"only {len(candles)} candles stored for "
                             f"{args.symbol} {args.timeframe}; backfill first"}
        cls = BASELINE_STRATEGIES[args.strategy]
        cfg = BacktestConfig(
            initial_equity=settings.risk.starting_equity_usdt,
            risk_pct_per_trade=settings.risk.max_risk_per_position_pct,
            max_notional_pct=settings.risk.max_position_notional_pct,
            costs=settings.costs, regime_cfg=settings.regime,
        )
        out: dict = {"strategy": args.strategy, "symbol": args.symbol,
                     "timeframe": args.timeframe, "bars": len(candles)}
        result = Backtester(cfg).run(cls(), candles)
        metrics = compute_metrics(result, initial_equity=cfg.initial_equity)
        out["in_sample"] = metrics
        if args.walk_forward:
            report = run_walk_forward(cls, cls.default_params(), candles, config=cfg)
            out["walk_forward"] = report.oos_metrics
            out["stability"] = report.stability
        if args.store:
            await BacktestRepository(db).store(
                str(uuid.uuid4()), strategy=args.strategy, strategy_version=cls.version,
                symbol=args.symbol, timeframe=args.timeframe, start=result.start,
                end=result.end, kind="in_sample", params=result.params, metrics=metrics,
                trades=[t.as_row() for t in result.trades], created_at=utcnow())
        return out
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=sorted(BASELINE_STRATEGIES))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--store", action="store_true")
    args = parser.parse_args()
    args.symbol = args.symbol.upper()
    print(json.dumps(asyncio.run(run(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
