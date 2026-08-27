"""In-process async pub/sub event bus with bounded per-subscriber queues.

Topics are plain strings. Two delivery policies:
- drop_oldest=True (market ticks): full queue drops the oldest item (latest data wins).
- drop_oldest=False (decision-critical events): publisher awaits space (backpressure).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class Topics:
    RAW_EVENT = "raw_event"
    CANDLE_CLOSED = "candle_closed"
    BOOK_TICKER = "book_ticker"
    TRADE = "trade"
    DEPTH = "depth"
    EXTERNAL_EVENT = "external_event"
    SIGNAL = "signal"
    DECISION = "decision"
    ORDER_UPDATE = "order_update"
    POSITION_UPDATE = "position_update"
    RISK_EVENT = "risk_event"
    REGIME_CHANGE = "regime_change"
    SYSTEM_EVENT = "system_event"
    NOTIFY = "notify"


@dataclass
class _Subscription:
    topic: str
    queue: asyncio.Queue[Any]
    drop_oldest: bool
    name: str
    dropped: int = 0


@dataclass
class EventBus:
    _subs: dict[str, list[_Subscription]] = field(default_factory=dict)

    def subscribe(
        self, topic: str, *, maxsize: int = 1000, drop_oldest: bool = False, name: str = "?"
    ) -> _Subscription:
        sub = _Subscription(topic, asyncio.Queue(maxsize=maxsize), drop_oldest, name)
        self._subs.setdefault(topic, []).append(sub)
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        subs = self._subs.get(sub.topic, [])
        if sub in subs:
            subs.remove(sub)

    async def publish(self, topic: str, item: Any) -> None:
        for sub in self._subs.get(topic, []):
            if sub.drop_oldest:
                while True:
                    try:
                        sub.queue.put_nowait(item)
                        break
                    except asyncio.QueueFull:
                        try:
                            sub.queue.get_nowait()
                            sub.dropped += 1
                        except asyncio.QueueEmpty:  # pragma: no cover - race
                            pass
            else:
                await sub.queue.put(item)

    async def iter(self, sub: _Subscription) -> AsyncIterator[Any]:
        while True:
            yield await sub.queue.get()

    def queue_sizes(self) -> dict[str, int]:
        return {
            f"{topic}:{s.name}": s.queue.qsize() for topic, subs in self._subs.items() for s in subs
        }
