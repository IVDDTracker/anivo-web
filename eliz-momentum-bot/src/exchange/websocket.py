"""Per-trade-session Binance futures WebSocket feed (wss://fstream.binance.com).

A `SymbolFeed` is opened when a signal fires and closed when the session ends:
combined streams `<sym>@aggTrade` + `<sym>@bookTicker` + `<sym>@depth5@100ms`.
Events are timestamped and pushed into async handlers (the momentum tracker).
Staleness is tracked so the kill switch can block trading on a dead feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import websockets

from src.core.clock import utcnow
from src.core.domain import AggTrade, BookTop
from src.core.logger import get_logger

log = get_logger(__name__)


def _ms(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts / 1000.0, tz=UTC)


class SymbolFeed:
    def __init__(self, ws_base: str, symbol: str, *,
                 on_trade: Callable[[AggTrade], Awaitable[None]] | None = None,
                 on_book: Callable[[BookTop], Awaitable[None]] | None = None,
                 on_depth: Callable[[list, list, datetime], Awaitable[None]] | None = None,
                 ) -> None:
        self.ws_base = ws_base.rstrip("/")
        self.symbol = symbol.upper()
        self.on_trade = on_trade
        self.on_book = on_book
        self.on_depth = on_depth
        self.last_event_at: datetime | None = None
        self.connected = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def url(self) -> str:
        s = self.symbol.lower()
        streams = f"{s}@aggTrade/{s}@bookTicker/{s}@depth5@100ms"
        return f"{self.ws_base}/stream?streams={streams}"

    def staleness_seconds(self, now: datetime) -> float:
        if self.last_event_at is None:
            return float("inf")
        return (now - self.last_event_at).total_seconds()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"feed:{self.symbol}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None,
                                              max_queue=4096) as ws:
                    self.connected = True
                    backoff = 0.5
                    log.info("feed connected for %s", self.symbol)
                    while not self._stop.is_set():
                        message = await ws.recv(decode=True)
                        await self.handle_raw(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.warning("feed %s dropped (%s); reconnecting in %.1fs",
                            self.symbol, str(exc)[:120], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        self.connected = False

    async def handle_raw(self, message: str | bytes) -> None:
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        await self.handle_message(payload)

    async def handle_message(self, payload: dict) -> None:
        """Transport-free handler (also driven directly by the backtest simulator)."""
        data = payload.get("data", payload)
        stream = payload.get("stream", "")
        event_type = data.get("e", "")
        try:
            if event_type == "aggTrade" and self.on_trade is not None:
                trade = AggTrade(price=float(data["p"]), qty=float(data["q"]),
                                 timestamp=_ms(int(data["T"])),
                                 is_buyer_maker=bool(data["m"]))
                self.last_event_at = utcnow()
                await self.on_trade(trade)
            elif event_type == "bookTicker" and self.on_book is not None:
                book = BookTop(bid_price=float(data["b"]), bid_qty=float(data["B"]),
                               ask_price=float(data["a"]), ask_qty=float(data["A"]),
                               timestamp=_ms(int(data["E"])) if data.get("E") else utcnow())
                self.last_event_at = utcnow()
                await self.on_book(book)
            elif ("depth" in stream or event_type == "depthUpdate") and self.on_depth is not None:
                bids = [[float(p), float(q)] for p, q in data.get("b", [])]
                asks = [[float(p), float(q)] for p, q in data.get("a", [])]
                self.last_event_at = utcnow()
                await self.on_depth(bids, asks, utcnow())
        except (KeyError, ValueError, TypeError):
            log.debug("malformed feed payload dropped for %s", self.symbol)
