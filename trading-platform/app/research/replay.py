"""Replay: reconstruct what the system knew at a point in time.

    python -m app.research.replay --date 2026-01-01 [--symbol BTCUSDT] [--timeframe 1h]

Uses SimClock pinned to the replay instant, so every component (feature engine,
regime classifier, event decay, strategies) sees exactly the knowledge available
then — the same structural guarantee that protects the backtester from
lookahead. Output is JSON on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from app.config.settings import get_settings
from app.core.clock import SimClock
from app.data.intelligence import EventIntelligence
from app.features.engine import compute_features
from app.regimes.classifier import classify
from app.storage.db import Database
from app.storage.repositories import CandleRepository, EventRepository
from app.strategies.base import StrategyContext
from app.strategies.baselines import BASELINE_STRATEGIES


async def replay(as_of: datetime, symbol: str, timeframe: str) -> dict:
    settings = get_settings()
    clock = SimClock(as_of)
    db = Database(settings.database_url)
    try:
        candles_repo = CandleRepository(db)
        events_repo = EventRepository(db)
        candles = await candles_repo.fetch(symbol, timeframe, end=as_of, limit=1200)
        candles = [c for c in candles if c.close_time <= as_of]
        features = compute_features(candles)
        regime = classify(candles, features, settings.regime)
        intelligence = EventIntelligence(events_repo, clock)
        evidence = await intelligence.evidence_score(symbol)
        recent_events = await events_repo.recent_external(
            since=as_of - __import__("datetime").timedelta(hours=48), asset=symbol, limit=20)
        recent_events = [e for e in recent_events if e.timestamp <= as_of]

        signals = []
        for cls in BASELINE_STRATEGIES.values():
            strategy = cls()
            if not candles:
                continue
            ctx = StrategyContext(
                symbol=symbol, timeframe=timeframe, now=as_of, candles=candles,
                features=features, regime=regime, event_evidence=evidence,
            )
            signal = strategy.generate_signal(ctx)
            if signal is not None:
                signals.append(signal.model_dump(mode="json"))

        return {
            "as_of": as_of.isoformat(),
            "symbol": symbol,
            "timeframe": timeframe,
            "candles_known": len(candles),
            "last_close": candles[-1].close if candles else None,
            "features": {k: (None if isinstance(v, float) and v != v else v)
                         for k, v in features.items()},
            "regime": {"regime": regime.regime, "volatility": regime.volatility_state,
                       "rules": regime.rules_fired},
            "external_evidence_score": evidence,
            "recent_external_events": [
                {"headline": e.headline, "source": e.source, "confidence": e.confidence,
                 "decayed_weight": e.decayed_weight(as_of)}
                for e in recent_events
            ],
            "signals_that_would_fire": signals,
        }
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True,
                        help="replay instant, e.g. 2026-01-01 or 2026-01-01T14:00")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="1h")
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.date)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    result = asyncio.run(replay(as_of, args.symbol.upper(), args.timeframe))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
