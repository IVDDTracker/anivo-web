"""Redis hot-state wrapper. Everything stored here is reconstructible from Postgres.

Keys:
  px:<symbol>          latest mark price (book-ticker mid) + timestamp
  health:<component>   heartbeat timestamps
  system:kill          kill-switch flag (any truthy value blocks new entries)
"""

from __future__ import annotations

import json
from datetime import datetime

import redis.asyncio as aioredis

from app.core.logging import get_logger

log = get_logger(__name__)


class HotState:
    def __init__(self, url: str) -> None:
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def healthcheck(self) -> bool:
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False

    async def set_price(self, symbol: str, price: float, ts: datetime) -> None:
        await self._redis.set(f"px:{symbol}", json.dumps({"p": price, "t": ts.isoformat()}))

    async def get_price(self, symbol: str) -> tuple[float, datetime] | None:
        raw = await self._redis.get(f"px:{symbol}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return float(data["p"]), datetime.fromisoformat(data["t"])
        except (KeyError, ValueError, json.JSONDecodeError):
            return None

    async def heartbeat(self, component: str, ts: datetime) -> None:
        await self._redis.set(f"health:{component}", ts.isoformat())

    async def last_heartbeat(self, component: str) -> datetime | None:
        raw = await self._redis.get(f"health:{component}")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def set_kill_switch(self, on: bool) -> None:
        if on:
            await self._redis.set("system:kill", "1")
        else:
            await self._redis.delete("system:kill")

    async def kill_switch_on(self) -> bool:
        """Fail-safe: if Redis is unreachable we report True (block new entries)."""
        try:
            return bool(await self._redis.get("system:kill"))
        except Exception:
            log.exception("redis unavailable while checking kill switch — failing safe")
            return True

    async def close(self) -> None:
        await self._redis.aclose()
