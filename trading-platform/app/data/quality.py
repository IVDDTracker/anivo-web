"""Data quality service: freshness, anomaly quarantine, quality scoring.

Fail-safe: quality can only be *earned* by fresh, sane data. Unknown symbol → 0.0.
An abnormal tick-to-tick jump (default 20%, config `abnormal_price_jump_pct`)
quarantines the symbol until `confirm_ticks` consecutive sane ticks arrive —
a single corrupt/fat-finger print cannot reach the decision pipeline
(the "50% instant price anomaly" chaos scenario).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.clock import Clock
from app.core.logging import get_logger
from app.core.state import StateMachine

log = get_logger(__name__)


@dataclass
class _SymbolQuality:
    last_price: float | None = None
    last_update: datetime | None = None
    quarantined: bool = False
    confirm_streak: int = 0
    pending_price: float | None = None
    anomalies: int = 0


@dataclass
class DataQualityService:
    clock: Clock
    state: StateMachine
    max_age_s: float = 120.0
    abnormal_jump_pct: float = 20.0
    confirm_ticks: int = 5
    _symbols: dict[str, _SymbolQuality] = field(default_factory=dict)

    def _sq(self, symbol: str) -> _SymbolQuality:
        return self._symbols.setdefault(symbol, _SymbolQuality())

    def on_price(self, symbol: str, price: float, ts: datetime) -> bool:
        """Feed a new observed price. Returns True if the price is accepted."""
        if price <= 0:
            return False
        sq = self._sq(symbol)
        if sq.last_price is None:
            sq.last_price, sq.last_update = price, ts
            return True
        jump_pct = abs(price / sq.last_price - 1.0) * 100.0
        if sq.quarantined:
            reference = sq.pending_price if sq.pending_price is not None else sq.last_price
            if abs(price / reference - 1.0) * 100.0 <= self.abnormal_jump_pct / 4:
                sq.confirm_streak += 1
                sq.pending_price = price
                if sq.confirm_streak >= self.confirm_ticks:
                    log.warning("symbol %s leaving quarantine at price %.8g", symbol, price)
                    sq.quarantined = False
                    sq.last_price, sq.last_update = price, ts
                    sq.pending_price, sq.confirm_streak = None, 0
                    return True
            else:
                sq.confirm_streak, sq.pending_price = 0, price
            return False
        if jump_pct > self.abnormal_jump_pct:
            sq.anomalies += 1
            sq.quarantined = True
            sq.pending_price, sq.confirm_streak = price, 1
            log.warning("abnormal price jump %.1f%% on %s (%.8g→%.8g): quarantined",
                        jump_pct, symbol, sq.last_price, price)
            return False
        sq.last_price, sq.last_update = price, ts
        return True

    def check_freshness(self) -> dict[str, bool]:
        """Recompute staleness for all symbols and sync the state machine."""
        now = self.clock.now()
        stale: dict[str, bool] = {}
        for symbol, sq in self._symbols.items():
            is_stale = (
                sq.last_update is None
                or (now - sq.last_update).total_seconds() > self.max_age_s
            )
            stale[symbol] = is_stale
            self.state.set_symbol_stale(symbol, is_stale)
        return stale

    def quality_score(self, symbol: str) -> float:
        """0..1 quality for the pipeline's DATA_QUALITY gate. Unknown → 0 (fail-safe)."""
        sq = self._symbols.get(symbol)
        if sq is None or sq.last_update is None or sq.last_price is None:
            return 0.0
        if sq.quarantined:
            return 0.0
        age = (self.clock.now() - sq.last_update).total_seconds()
        if age > self.max_age_s:
            return 0.0
        freshness = max(0.0, 1.0 - age / self.max_age_s)
        return round(0.5 + 0.5 * freshness, 4)

    def last_price(self, symbol: str) -> float | None:
        sq = self._symbols.get(symbol)
        return sq.last_price if sq and not sq.quarantined else None

    def snapshot(self) -> dict[str, dict]:
        now = self.clock.now()
        return {
            sym: {
                "last_price": sq.last_price,
                "age_s": None if sq.last_update is None
                else round((now - sq.last_update).total_seconds(), 1),
                "quarantined": sq.quarantined,
                "anomalies": sq.anomalies,
                "quality": self.quality_score(sym),
            }
            for sym, sq in self._symbols.items()
        }
