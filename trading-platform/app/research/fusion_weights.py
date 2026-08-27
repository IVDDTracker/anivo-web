"""Fusion-weight research harness (DECISIONS.md D-014).

Evaluates candidate component-weight sets by the predictive rank-correlation of
the fused evidence score against forward returns on purged walk-forward splits.
This is the ONLY sanctioned path to changing fusion weights: run it, record the
run, then change `app/signals/fusion.py` with a reference to the run.

    python -m app.research.fusion_weights --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math

import numpy as np

from app.backtest.walkforward import MAX_FEATURE_LOOKBACK, make_windows
from app.config.settings import get_settings
from app.features.engine import compute_features
from app.models.market import Candle
from app.storage.db import Database
from app.storage.repositories import CandleRepository

CANDIDATE_WEIGHT_SETS = {
    "prior-v1(equal)": {"technical": 1.0, "momentum": 1.0, "volume": 1.0},
    "momentum-heavy": {"technical": 0.5, "momentum": 2.0, "volume": 0.5},
    "technical-heavy": {"technical": 2.0, "momentum": 0.5, "volume": 0.5},
}


def _component_scores(features: dict) -> dict[str, float]:
    def sig(x: float, scale: float) -> float:
        if x != x:
            return 0.0
        return 2.0 / (1.0 + math.exp(-x / scale)) - 1.0

    return {
        "technical": sig((features.get("ema_structure_bull", 0.0) - 0.5)
                         + (features.get("bb_pct_b", 0.5) - 0.5), 0.7),
        "momentum": sig(features.get("momentum_20", 0.0) * 100.0, 3.0),
        "volume": sig(features.get("volume_zscore", 0.0), 1.5),
    }


def _rank_ic(scores: list[float], fwd_returns: list[float]) -> float:
    if len(scores) < 30:
        return float("nan")
    a = np.argsort(np.argsort(scores)).astype(float)
    b = np.argsort(np.argsort(fwd_returns)).astype(float)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def evaluate_weights(candles: list[Candle], *, horizon_bars: int = 24,
                     test_bars: int = 24 * 30) -> dict:
    """Rank-IC of each candidate weight set on out-of-sample windows only."""
    n = len(candles)
    windows = make_windows(n - horizon_bars, train_bars=24 * 90, test_bars=test_bars,
                           step_bars=test_bars, purge_bars=MAX_FEATURE_LOOKBACK)
    results: dict[str, list[float]] = {name: [] for name in CANDIDATE_WEIGHT_SETS}
    for w in windows:
        per_set_scores: dict[str, list[float]] = {name: [] for name in CANDIDATE_WEIGHT_SETS}
        fwd: list[float] = []
        for i in range(w.test_start, w.test_end, 6):  # every 6 bars to decorrelate
            features = compute_features(candles[: i + 1])
            if not features:
                continue
            future = candles[i + horizon_bars].close if i + horizon_bars < n else None
            if future is None:
                continue
            fwd.append(future / candles[i].close - 1.0)
            comps = _component_scores(features)
            for name, weights in CANDIDATE_WEIGHT_SETS.items():
                total_w = sum(weights.values())
                per_set_scores[name].append(
                    sum(comps[k] * wt for k, wt in weights.items()) / total_w)
        for name in CANDIDATE_WEIGHT_SETS:
            ic = _rank_ic(per_set_scores[name], fwd)
            if ic == ic:
                results[name].append(ic)
    return {
        name: {"mean_oos_rank_ic": round(float(np.mean(ics)), 4) if ics else None,
               "windows": len(ics)}
        for name, ics in results.items()
    }


async def run(symbol: str, timeframe: str) -> dict:
    settings = get_settings()
    db = Database(settings.database_url)
    try:
        candles = await CandleRepository(db).fetch(symbol, timeframe)
        if len(candles) < 24 * 120:
            return {"error": f"need ≥ {24*120} bars, have {len(candles)}"}
        return evaluate_weights(candles)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="1h")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.symbol.upper(), args.timeframe)), indent=2))


if __name__ == "__main__":
    main()
