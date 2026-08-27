"""Test helpers: synthetic candle generation (deterministic)."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from app.models.market import Candle


def make_candles(
    n: int,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    start_price: float = 50_000.0,
    drift: float = 0.0,
    vol: float = 0.005,
    seed: int = 7,
    start: datetime | None = None,
    volume: float = 100.0,
) -> list[Candle]:
    rng = random.Random(seed)
    start = start or datetime(2025, 1, 1, tzinfo=UTC)
    step = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    out: list[Candle] = []
    price = start_price
    for i in range(n):
        ret = drift + rng.gauss(0, vol)
        open_p = price
        close_p = price * math.exp(ret)
        high = max(open_p, close_p) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_p, close_p) * (1 - abs(rng.gauss(0, vol / 2)))
        vol_mult = 1 + abs(rng.gauss(0, 0.3))
        open_time = start + timedelta(minutes=step * i)
        out.append(
            Candle(
                symbol=symbol, timeframe=timeframe,
                open_time=open_time, close_time=open_time + timedelta(minutes=step),
                open=open_p, high=high, low=low, close=close_p,
                volume=volume * vol_mult, quote_volume=volume * vol_mult * close_p,
                trades=100, taker_buy_base=volume * vol_mult * 0.5,
            )
        )
        price = close_p
    return out
