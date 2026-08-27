"""WS collector behavior incl. chaos scenarios: duplicates, out-of-order, gaps, shutdown."""

from __future__ import annotations

import pytest

from app.core.bus import EventBus, Topics
from app.data.collectors.binance_ws import BinanceWsCollector
from app.storage.repositories import CandleRepository, EventRepository
from tests.test_binance_normalization import KLINE_EVENT, TRADE_EVENT


class FakeRest:
    def __init__(self):
        self.backfill_calls: list[tuple] = []

    async def klines(self, symbol, timeframe, *, start_ms=None, end_ms=None, limit=1000):
        self.backfill_calls.append((symbol, timeframe, start_ms, end_ms))
        return []


@pytest.fixture
def collector(db):
    bus = EventBus()
    return BinanceWsCollector(
        ws_base="wss://example.invalid", symbols=["BTCUSDT"], timeframes=["1m"],
        bus=bus, events=EventRepository(db), candles=CandleRepository(db),
        rest=FakeRest(),
    ), bus, db


def _wrap(data, stream="btcusdt@kline_1m"):
    return {"stream": stream, "data": data}


async def test_closed_kline_persisted_and_published(collector):
    coll, bus, db = collector
    sub = bus.subscribe(Topics.CANDLE_CLOSED, name="t")
    await coll.handle_message(_wrap(KLINE_EVENT))
    candle = sub.queue.get_nowait()
    assert candle.symbol == "BTCUSDT" and candle.closed
    stored = await CandleRepository(db).fetch("BTCUSDT", "1m")
    assert len(stored) == 1


async def test_duplicate_kline_dropped(collector):
    coll, bus, db = collector
    sub = bus.subscribe(Topics.CANDLE_CLOSED, name="t")
    await coll.handle_message(_wrap(KLINE_EVENT))
    await coll.handle_message(_wrap(KLINE_EVENT))  # duplicate WS message
    assert sub.queue.qsize() == 1
    assert coll.health.duplicates_dropped == 1
    assert len(await CandleRepository(db).fetch("BTCUSDT", "1m")) == 1


async def test_duplicate_survives_lru_wipe_via_db_constraint(collector):
    coll, bus, db = collector
    await coll.handle_message(_wrap(KLINE_EVENT))
    coll._recent_hashes.clear()  # simulate restart / LRU eviction
    await coll.handle_message(_wrap(KLINE_EVENT))
    assert coll.health.duplicates_dropped == 1
    assert len(await CandleRepository(db).fetch("BTCUSDT", "1m")) == 1


async def test_gap_triggers_backfill(collector):
    coll, bus, db = collector
    await coll.handle_message(_wrap(KLINE_EVENT))
    later = {**KLINE_EVENT, "k": {**KLINE_EVENT["k"],
                                  "t": KLINE_EVENT["k"]["t"] + 3 * 60_000,
                                  "T": KLINE_EVENT["k"]["T"] + 3 * 60_000}}
    await coll.handle_message(_wrap(later))
    assert len(coll.rest.backfill_calls) == 1
    _, _, start_ms, end_ms = coll.rest.backfill_calls[0]
    assert start_ms == KLINE_EVENT["k"]["t"] + 60_000
    assert end_ms == KLINE_EVENT["k"]["t"] + 3 * 60_000 - 1


async def test_out_of_order_kline_does_not_rewind_gap_tracking(collector):
    coll, bus, db = collector
    later = {**KLINE_EVENT, "k": {**KLINE_EVENT["k"],
                                  "t": KLINE_EVENT["k"]["t"] + 60_000,
                                  "T": KLINE_EVENT["k"]["T"] + 60_000}}
    await coll.handle_message(_wrap(later))
    await coll.handle_message(_wrap(KLINE_EVENT))  # old candle arrives late
    key = ("BTCUSDT", "1m")
    assert coll._last_closed_kline_ms[key] == later["k"]["t"]


async def test_trade_dedup(collector):
    coll, bus, db = collector
    sub = bus.subscribe(Topics.TRADE, name="t")
    await coll.handle_message(_wrap(TRADE_EVENT, stream="btcusdt@trade"))
    await coll.handle_message(_wrap(TRADE_EVENT, stream="btcusdt@trade"))
    assert sub.queue.qsize() == 1


async def test_malformed_message_counted_not_fatal(collector):
    coll, bus, db = collector
    await coll.handle_message(_wrap({"e": "kline", "k": {"bogus": True}}))
    assert coll.health.parse_errors == 1


async def test_server_shutdown_forces_reconnect(collector):
    coll, bus, db = collector
    with pytest.raises(ConnectionResetError):
        await coll.handle_message({"stream": "x", "data": {"e": "serverShutdown", "E": 1}})


def test_stream_names_within_connection_limit(collector):
    coll, _, _ = collector
    names = coll.stream_names()
    assert len(names) <= 1024  # official per-connection stream cap
    assert "btcusdt@kline_1m" in names and "btcusdt@bookTicker" in names
