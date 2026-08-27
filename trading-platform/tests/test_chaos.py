"""Additional chaos scenarios (others live next to their components):

- duplicate WS floods do not grow storage
- future/incorrect external timestamps cannot amplify evidence
- DB failure during order submission leaves no phantom in-memory position
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.bus import EventBus
from app.data.collectors.binance_ws import BinanceWsCollector
from app.data.normalization.events import build_external_event
from app.models.enums import SourceType, Venue
from app.storage.repositories import CandleRepository, EventRepository, OrderRepository
from tests.test_binance_normalization import KLINE_EVENT


async def test_duplicate_ws_flood_stores_once(db):
    coll = BinanceWsCollector(
        ws_base="wss://x", symbols=["BTCUSDT"], timeframes=["1m"], bus=EventBus(),
        events=EventRepository(db), candles=CandleRepository(db), rest=None)
    for _ in range(200):
        await coll.handle_message({"stream": "btcusdt@kline_1m", "data": KLINE_EVENT})
    assert len(await CandleRepository(db).fetch("BTCUSDT", "1m")) == 1
    assert coll.health.duplicates_dropped == 199


def test_future_timestamp_cannot_amplify_evidence(sim_clock):
    event = build_external_event(
        text="Exchange hacked!!", source="rss:x", source_type=SourceType.NEWS,
        reliability=0.7, timestamp=sim_clock.now() + timedelta(days=3))  # broken clock upstream
    weight_now = event.decayed_weight(sim_clock.now())
    weight_at_event = event.decayed_weight(event.timestamp)
    assert weight_now <= weight_at_event  # negative age clamps to 0, never boosts


async def test_db_failure_during_submit_leaves_no_phantom_position(sim_clock):
    from sqlalchemy.exc import OperationalError

    from app.config.settings import CostConfig
    from app.models.enums import Direction, OrderSide, OrderType
    from app.models.market import BookTicker
    from app.models.orders import TradeIntent
    from app.paper.engine import PaperEngine
    from app.storage.db import Database

    broken_db = Database("sqlite+aiosqlite:////nonexistent-dir/broken/x.db")
    repo = OrderRepository(broken_db, Venue.PAPER)
    engine = PaperEngine(costs=CostConfig(latency_ms=0), clock=sim_clock, repo=repo,
                         starting_cash=Decimal("10000"))
    engine._last_ticker["BTCUSDT"] = BookTicker(
        symbol="BTCUSDT", bid_price=100, bid_qty=5, ask_price=100.1, ask_qty=5,
        timestamp=sim_clock.now())
    intent = TradeIntent(
        signal_id="s", symbol="BTCUSDT", direction=Direction.LONG, side=OrderSide.BUY,
        order_type=OrderType.MARKET, reference_price=Decimal("100"),
        quantity=Decimal("1"), venue=Venue.PAPER, strategy="t", created_at=sim_clock.now())
    with pytest.raises(OperationalError):
        await engine.submit(intent)  # DB down → intent persist fails BEFORE any order
    # fail-safe: nothing half-created in memory
    assert engine.positions == {}
    assert engine.open_orders == {}
    assert engine.cash == Decimal("10000")
