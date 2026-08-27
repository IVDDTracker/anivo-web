"""Seed demo data (candles, a signal, a decision, positions, snapshots) so the
dashboard has content before live data accumulates.

    python -m scripts.seed_demo
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import timedelta
from decimal import Decimal

from app.config.settings import get_settings
from app.core.clock import utcnow
from app.models.enums import Direction, PipelineStage, Regime, SignalDecision, Venue
from app.models.market import Candle
from app.models.orders import Position
from app.models.signals import DecisionRecord, EvidenceItem, Signal, StageResult
from app.storage.db import Database
from app.storage.repositories import (
    CandleRepository,
    OrderRepository,
    SignalRepository,
    SystemRepository,
)


def synth_candles(symbol: str, n: int = 600, start_price: float = 60_000.0) -> list[Candle]:
    rng = random.Random(hash(symbol) % 1000)
    start = utcnow() - timedelta(hours=n)
    out, price = [], start_price
    for i in range(n):
        ret = 0.0002 + rng.gauss(0, 0.006)
        open_p, close_p = price, price * math.exp(ret)
        high = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.002)))
        low = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.002)))
        t0 = start + timedelta(hours=i)
        out.append(Candle(symbol=symbol, timeframe="1h", open_time=t0,
                          close_time=t0 + timedelta(hours=1), open=open_p, high=high,
                          low=low, close=close_p, volume=100 * (1 + abs(rng.gauss(0, .3))),
                          quote_volume=100 * close_p, trades=500,
                          taker_buy_base=50))
        price = close_p
    return out


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_all()
    candles = CandleRepository(db)
    signals = SignalRepository(db)
    system = SystemRepository(db)
    paper = OrderRepository(db, Venue.PAPER)
    now = utcnow()

    for symbol, px in (("BTCUSDT", 60_000.0), ("ETHUSDT", 3_000.0), ("SOLUSDT", 150.0)):
        for c in synth_candles(symbol, 600, px):
            await candles.upsert(c)

    signal = Signal(symbol="BTCUSDT", strategy="trend_momentum", strategy_version="1.0",
                    direction=Direction.LONG, timestamp=now - timedelta(hours=2),
                    reference_price=60_500.0, confidence=78,
                    hypothetical_stop=59_200.0, hypothetical_target=63_400.0,
                    market_regime=Regime.WEAK_UPTREND,
                    evidence=[EvidenceItem(name="price above trend structure", value=True),
                              EvidenceItem(name="positive volume acceleration", value=0.4)],
                    risks=["volatility elevated"])
    await signals.store_signal(signal)
    record = DecisionRecord(signal_id=signal.id, symbol="BTCUSDT", strategy="trend_momentum",
                            timestamp=signal.timestamp, decision=SignalDecision.APPROVED,
                            venue=Venue.PAPER,
                            stages=[StageResult(stage=s, passed=True, reasons=["ok"])
                                    for s in PipelineStage],
                            explanation="demo decision")
    await signals.store_decision(record)

    open_pos = Position(venue=Venue.PAPER, symbol="BTCUSDT", qty=Decimal("0.03"),
                        avg_entry_price=Decimal("60500"), stop_price=Decimal("59200"),
                        target_price=Decimal("63400"), strategy="trend_momentum",
                        signal_id=signal.id, opened_at=now - timedelta(hours=2))
    await paper.store_position(open_pos)
    closed = Position(venue=Venue.PAPER, symbol="ETHUSDT", qty=Decimal("0"),
                      avg_entry_price=Decimal("2980"), strategy="range_mean_reversion",
                      opened_at=now - timedelta(hours=30),
                      closed_at=now - timedelta(hours=20),
                      realized_pnl=Decimal("42.5"), fees_paid=Decimal("3.1"),
                      close_reason="target")
    await paper.store_position(closed)

    equity = 10_000.0
    for i in range(96):
        ts = now - timedelta(hours=96 - i)
        equity *= 1 + random.Random(i).gauss(0.0002, 0.002)
        await system.performance_snapshot(ts, "PAPER", Decimal(str(round(equity, 2))),
                                          Decimal("8000"), Decimal("50"), Decimal("12"),
                                          max(0.0, (10_200 - equity) / 10_200 * 100), 1)
    await system.regime_change(now - timedelta(hours=1), "BTCUSDT", "1h",
                               "WEAK_UPTREND", "normal", {"rules": ["demo"]})
    await db.dispose()
    print("demo data seeded")


if __name__ == "__main__":
    asyncio.run(main())
