"""Injected clock. Live uses RealClock; the backtest simulator uses SimClock,
so strategy code can never accidentally read wall-clock time in a backtest
(structural look-ahead protection)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be tz-aware UTC")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        if when < self._now:
            raise ValueError("SimClock cannot go backwards")
        self._now = when

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
