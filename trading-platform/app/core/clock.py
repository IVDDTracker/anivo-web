"""Injected clock abstraction.

Decision-making code never calls datetime.now() directly; it asks the injected Clock.
Backtest/replay inject SimClock, which makes lookahead structurally impossible:
"now" is always the simulation time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol


def utcnow() -> datetime:
    """Wall-clock UTC. Infrastructure-only (logging, received-timestamps)."""
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
    """Deterministic clock for backtests and replay. Sleeps advance time instantly."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware (UTC)")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        if when < self._now:
            raise ValueError(f"SimClock cannot go backwards: {when} < {self._now}")
        self._now = when

    async def sleep(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
