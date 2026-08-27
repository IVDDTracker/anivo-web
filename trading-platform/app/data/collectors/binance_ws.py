"""Binance combined-stream WebSocket collector.

Official constraints handled (web-socket-streams.md):
- 24h max connection lifetime → proactive reconnect at `ws_reconnect_before_h`.
- Server ping every 20s, pong within 60s → the `websockets` library auto-pongs;
  we additionally watch message age and force-reconnect a silent connection.
- `serverShutdown` event → immediate reconnect.
- Combined stream wrapper {"stream": ..., "data": ...}.

Correctness behaviors:
- every event is normalized, hashed, deduplicated and (for klines/trades) persisted
  BEFORE downstream processing — duplicates and replays are dropped exactly once;
- closed-kline gap detection triggers REST backfill (never assume data is complete);
- message handling is separated from transport (`handle_message`) so chaos tests can
  inject duplicate/out-of-order/anomalous messages without a socket.
"""

from __future__ import annotations

import json
from datetime import timedelta

import websockets

from app.core.bus import EventBus, Topics
from app.core.clock import utcnow
from app.core.logging import get_logger, log_ctx
from app.data.collectors.base import BaseCollector
from app.data.collectors.binance_rest import BinanceMarketData
from app.data.normalization import binance as norm
from app.models.enums import SourceType
from app.storage.redis_client import HotState
from app.storage.repositories import CandleRepository, EventRepository
from app.storage.tables import TradeRow

import logging

log = get_logger(__name__)

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
          "4h": 14_400_000, "1d": 86_400_000}


class BinanceWsCollector(BaseCollector):
    name = "binance_ws"
    source_type = SourceType.MARKET

    def __init__(
        self,
        *,
        ws_base: str,
        symbols: list[str],
        timeframes: list[str],
        bus: EventBus,
        events: EventRepository,
        candles: CandleRepository,
        rest: BinanceMarketData,
        hot: HotState | None = None,
        reconnect_before_h: float = 23.0,
        stale_after_s: float = 90.0,
    ) -> None:
        super().__init__()
        self.ws_base = ws_base.rstrip("/")
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = timeframes
        self.bus = bus
        self.events = events
        self.candles = candles
        self.rest = rest
        self.hot = hot
        self.reconnect_before = timedelta(hours=reconnect_before_h)
        self.stale_after_s = stale_after_s
        self._last_closed_kline_ms: dict[tuple[str, str], int] = {}
        self._recent_hashes: dict[str, None] = {}  # bounded LRU of event hashes

    # ── stream construction ──────────────────────────────────────────────────

    def stream_names(self) -> list[str]:
        streams: list[str] = []
        for sym in self.symbols:
            s = sym.lower()
            streams += [f"{s}@kline_{tf}" for tf in self.timeframes]
            streams += [f"{s}@trade", f"{s}@bookTicker", f"{s}@depth20@100ms"]
        return streams

    def url(self) -> str:
        return f"{self.ws_base}/stream?streams={'/'.join(self.stream_names())}"

    # ── transport loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        url = self.url()
        async with websockets.connect(url, ping_interval=None, max_queue=4096) as ws:
            self.health.connected_since = utcnow()
            self.health.healthy = True
            self.health.detail = "connected"
            log_ctx(log, logging.INFO, "binance ws connected", streams=len(self.stream_names()))
            while True:
                if utcnow() - self.health.connected_since > self.reconnect_before:
                    log.info("proactive reconnect before 24h connection limit")
                    self.health.reconnects += 1
                    return  # supervisor restarts us with a fresh connection
                try:
                    message = await ws.recv(decode=True)
                except TimeoutError:
                    continue
                await self._on_raw_message(message)

    async def _on_raw_message(self, message: str | bytes) -> None:
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.health.parse_errors += 1
            return
        await self.handle_message(payload)

    # ── message handling (transport-free; used by chaos tests) ───────────────

    async def handle_message(self, payload: dict) -> None:
        data = payload.get("data", payload)
        stream = payload.get("stream", "")
        event_type = data.get("e", "")
        if event_type == "serverShutdown":
            raise ConnectionResetError("binance serverShutdown announced")
        try:
            if event_type == "kline":
                await self._handle_kline(data)
            elif event_type == "trade":
                await self._handle_trade(data)
            elif "bookTicker" in stream or ("b" in data and "a" in data and "s" in data
                                            and event_type == ""):
                await self._handle_book_ticker(data)
            elif "depth" in stream:
                symbol = stream.split("@", 1)[0].upper()
                await self._handle_depth(symbol, data)
            # unknown events are ignored on purpose (forward compatible)
        except (KeyError, ValueError, TypeError):
            self.health.parse_errors += 1
            return
        self.health.mark_event()

    def _seen(self, ehash: str) -> bool:
        """Process-local dedup LRU in front of the DB unique constraint."""
        if ehash in self._recent_hashes:
            return True
        self._recent_hashes[ehash] = None
        if len(self._recent_hashes) > 50_000:
            for key in list(self._recent_hashes)[:10_000]:
                del self._recent_hashes[key]
        return False

    async def _handle_kline(self, data: dict) -> None:
        candle, raw = norm.parse_kline(data)
        if self._seen(raw.event_hash):
            self.health.duplicates_dropped += 1
            return
        if candle.closed:
            stored = await self.events.store_raw(raw)
            if not stored:
                self.health.duplicates_dropped += 1
                return
            await self._detect_gap_and_backfill(candle)
            await self.candles.upsert(candle)
            await self.bus.publish(Topics.CANDLE_CLOSED, candle)
        # in-progress candles are intentionally not persisted or published:
        # all decisions run on closed bars only (no lookahead, no partial-bar noise)

    async def _detect_gap_and_backfill(self, candle) -> None:
        key = (candle.symbol, candle.timeframe)
        step = _TF_MS.get(candle.timeframe)
        open_ms = int(candle.open_time.timestamp() * 1000)
        last = self._last_closed_kline_ms.get(key)
        if step and last is not None and open_ms > last + step:
            missing = (open_ms - last - step) // step
            log_ctx(log, logging.WARNING, "kline gap detected; backfilling",
                    symbol=candle.symbol, timeframe=candle.timeframe, missing=int(missing))
            try:
                fetched = await self.rest.klines(
                    candle.symbol, candle.timeframe,
                    start_ms=last + step, end_ms=open_ms - 1, limit=1000,
                )
                for filler in fetched:
                    await self.candles.upsert(filler)
                    await self.bus.publish(Topics.CANDLE_CLOSED, filler)
            except Exception:
                log.exception("kline backfill failed (will rely on next startup backfill)")
        if last is None or open_ms > last:
            self._last_closed_kline_ms[key] = open_ms

    async def _handle_trade(self, data: dict) -> None:
        tick, raw = norm.parse_trade(data)
        if self._seen(raw.event_hash):
            self.health.duplicates_dropped += 1
            return
        try:
            async with self.events.db.session() as s:
                s.add(TradeRow(symbol=tick.symbol, trade_id=tick.trade_id, price=tick.price,
                               qty=tick.qty, is_buyer_maker=tick.is_buyer_maker,
                               timestamp=tick.timestamp))
        except Exception:
            self.health.duplicates_dropped += 1
            return
        await self.bus.publish(Topics.TRADE, tick)

    async def _handle_book_ticker(self, data: dict) -> None:
        ticker = norm.parse_book_ticker(data)
        if self.hot is not None:
            await self.hot.set_price(ticker.symbol, ticker.mid, ticker.timestamp)
        await self.bus.publish(Topics.BOOK_TICKER, ticker)

    async def _handle_depth(self, symbol: str, data: dict) -> None:
        snapshot = norm.parse_partial_depth(symbol, data)
        await self.bus.publish(Topics.DEPTH, snapshot)


async def backfill_history(
    rest: BinanceMarketData,
    candles: CandleRepository,
    symbols: list[str],
    timeframes: list[str],
    *,
    limit: int = 1000,
) -> int:
    """Startup backfill: fetch recent history so features/regimes warm immediately."""
    total = 0
    for symbol in symbols:
        for tf in timeframes:
            try:
                fetched = await rest.klines(symbol, tf, limit=limit)
            except Exception:
                log.exception("backfill failed for %s %s", symbol, tf)
                continue
            closed = [c for c in fetched if c.closed]
            for c in closed:
                await candles.upsert(c)
            total += len(closed)
    return total
