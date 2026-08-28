"""Leg exit management (spec §8/§9).

- LongLegManager: hard stop + max holding time (the reversal-score exit itself
  is evaluated by the session loop; this covers protective exits).
- ShortConfirmation: LONG closing does NOT auto-open a SHORT — the reversal must
  SUSTAIN for `short_confirmation_seconds`, price must sit below session VWAP,
  and the bounce off the long-exit price must stay small. If price recovers or
  the confirmation window passes → no short (DONE).
- ShortLegManager: SL / TP / trailing stop / max holding for the short leg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.core.config import Settings, ShortParams
from src.strategy.momentum import MomentumMetrics
from src.strategy.reversal import ReversalReading


@dataclass
class LongLegManager:
    entry_price: float
    entry_time: datetime
    stop_pct: float
    max_holding_seconds: float
    stop_price: float = field(init=False)

    def __post_init__(self) -> None:
        self.stop_price = self.entry_price * (1.0 - self.stop_pct / 100.0)

    def protective_exit(self, price: float, now: datetime) -> str | None:
        if price <= self.stop_price:
            return "stop_loss"
        if (now - self.entry_time).total_seconds() >= self.max_holding_seconds:
            return "max_holding_time"
        return None


@dataclass
class ShortConfirmation:
    """Separate SHORT_CONFIRMATION check (spec §8): sustained, behavior-based."""

    cfg: Settings
    long_exit_price: float
    started_at: datetime
    _sustained_since: datetime | None = None

    def evaluate(self, reading: ReversalReading, metrics: MomentumMetrics,
                 now: datetime) -> str:
        """Returns 'confirm' | 'reject' | 'wait'."""
        window = getattr(self.cfg, "short_confirmation_window_s", 45.0)
        if (now - self.started_at).total_seconds() > window:
            return "reject"  # momentum didn't confirm in time → stand down
        bounce_pct = ((metrics.current_price / self.long_exit_price - 1.0) * 100.0
                      if self.long_exit_price > 0 else 0.0)
        if bounce_pct > self.cfg.short_params.entry_max_bounce_pct:
            return "reject"  # buyers stepped back in → no short
        sustained_ok = reading.score >= self.cfg.min_reversal_score
        below_vwap = metrics.price_vs_vwap_pct < 0.0
        if sustained_ok and below_vwap:
            if self._sustained_since is None:
                self._sustained_since = now
            if (now - self._sustained_since).total_seconds() >= \
                    self.cfg.short_confirmation_seconds:
                return "confirm"
        else:
            self._sustained_since = None
        return "wait"


@dataclass
class ShortLegManager:
    entry_price: float
    entry_time: datetime
    params: ShortParams
    max_holding_seconds: float
    lowest_price: float = field(init=False)
    stop_price: float = field(init=False)
    take_profit_price: float = field(init=False)
    trailing_armed: bool = False

    def __post_init__(self) -> None:
        self.lowest_price = self.entry_price
        self.stop_price = self.entry_price * (1.0 + self.params.stop_loss_pct / 100.0)
        self.take_profit_price = self.entry_price * (1.0 - self.params.take_profit_pct / 100.0)

    def check(self, price: float, now: datetime) -> str | None:
        """Update trailing state and return an exit reason or None (hold)."""
        if price < self.lowest_price:
            self.lowest_price = price
        profit_pct = (self.entry_price - price) / self.entry_price * 100.0
        best_profit_pct = (self.entry_price - self.lowest_price) / self.entry_price * 100.0

        if price >= self.stop_price:
            return "stop_loss"
        if price <= self.take_profit_price:
            return "take_profit"
        if not self.trailing_armed and best_profit_pct >= self.params.trailing_activation_pct:
            self.trailing_armed = True
        if self.trailing_armed:
            trail_stop = self.lowest_price * (1.0 + self.params.trailing_stop_pct / 100.0)
            if price >= trail_stop:
                return "trailing_stop"
        if (now - self.entry_time).total_seconds() >= self.max_holding_seconds:
            return "max_holding_time"
        del profit_pct
        return None
