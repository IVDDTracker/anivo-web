"""Hard kill switch (spec §11): any tripped condition blocks NEW trades.

Fail-safe: conditions must be affirmatively healthy; unknown = tripped.
Existing positions are still managed (exits are never blocked).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class KillSwitch:
    _tripped: dict[str, str] = field(default_factory=dict)
    fired_ever: bool = False

    def trip(self, condition: str, detail: str) -> None:
        self._tripped[condition] = detail
        self.fired_ever = True

    def clear(self, condition: str) -> None:
        self._tripped.pop(condition, None)

    @property
    def active(self) -> bool:
        return bool(self._tripped)

    @property
    def reasons(self) -> dict[str, str]:
        return dict(self._tripped)

    def check_feed(self, staleness_s: float, max_staleness_s: float) -> None:
        if staleness_s > max_staleness_s:
            self.trip("STALE_MARKET_DATA", f"feed stale {staleness_s:.1f}s")
        else:
            self.clear("STALE_MARKET_DATA")

    def check_latency(self, latency_ms: float, max_ms: float) -> None:
        if latency_ms > max_ms:
            self.trip("EXCESSIVE_LATENCY", f"{latency_ms:.0f}ms > {max_ms:.0f}ms")
        else:
            self.clear("EXCESSIVE_LATENCY")

    def snapshot(self, now: datetime) -> dict:
        return {"active": self.active, "reasons": self.reasons,
                "as_of": now.isoformat()}
