"""Collector plug-in interface and per-source health tracking."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.clock import utcnow
from app.models.enums import SourceType


@dataclass
class CollectorHealth:
    name: str
    healthy: bool = False
    detail: str = "not started"
    events_seen: int = 0
    duplicates_dropped: int = 0
    parse_errors: int = 0
    last_event_at: datetime | None = None
    connected_since: datetime | None = None
    reconnects: int = 0

    def lag_seconds(self) -> float:
        if self.last_event_at is None:
            return float("inf")
        return max(0.0, (utcnow() - self.last_event_at).total_seconds())

    def mark_event(self) -> None:
        self.events_seen += 1
        self.last_event_at = utcnow()

    def as_dict(self) -> dict:
        lag = self.lag_seconds()
        return {
            "name": self.name,
            "healthy": self.healthy,
            "detail": self.detail,
            "events_seen": self.events_seen,
            "duplicates_dropped": self.duplicates_dropped,
            "parse_errors": self.parse_errors,
            "lag_seconds": None if lag == float("inf") else round(lag, 1),
            "reconnects": self.reconnects,
        }


class BaseCollector(ABC):
    """Every data source implements this interface and is run under the Supervisor."""

    name: str = "base"
    source_type: SourceType = SourceType.MARKET

    def __init__(self) -> None:
        self.health = CollectorHealth(name=self.name)

    @abstractmethod
    async def run(self) -> None:
        """Long-running loop. Exceptions are caught by the supervisor (restart w/ backoff)."""

    async def healthcheck(self, *, stale_after_s: float = 300.0) -> CollectorHealth:
        if self.health.last_event_at is None:
            self.health.healthy = False
        elif self.health.lag_seconds() > stale_after_s:
            self.health.healthy = False
            self.health.detail = f"no events for {self.health.lag_seconds():.0f}s"
        return self.health


def freshness_ok(last_event_at: datetime | None, max_age: timedelta, now: datetime) -> bool:
    return last_event_at is not None and (now - last_event_at) <= max_age
