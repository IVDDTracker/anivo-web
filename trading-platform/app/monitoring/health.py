"""Aggregate health service: collectors, DB, Redis, connectivity, queues, lag."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.bus import EventBus
from app.core.clock import Clock
from app.core.state import StateMachine
from app.data.collectors.base import BaseCollector
from app.data.quality import DataQualityService
from app.storage.db import Database
from app.storage.redis_client import HotState


@dataclass
class HealthService:
    clock: Clock
    state: StateMachine
    db: Database | None = None
    hot: HotState | None = None
    bus: EventBus | None = None
    quality: DataQualityService | None = None
    collectors: list[BaseCollector] = field(default_factory=list)
    telegram_ok: bool | None = None

    async def snapshot(self) -> dict:
        collectors = {}
        for collector in self.collectors:
            health = await collector.healthcheck()
            collectors[collector.name] = health.as_dict()
        return {
            "state": self.state.status().__dict__,
            "database": await self.db.healthcheck() if self.db else None,
            "redis": await self.hot.healthcheck() if self.hot else None,
            "telegram": self.telegram_ok,
            "collectors": collectors,
            "queues": self.bus.queue_sizes() if self.bus else {},
            "data_quality": self.quality.snapshot() if self.quality else {},
        }

    async def overall_ok(self) -> bool:
        snap = await self.snapshot()
        db_ok = snap["database"] is not False
        redis_ok = snap["redis"] is not False
        return db_ok and redis_ok
