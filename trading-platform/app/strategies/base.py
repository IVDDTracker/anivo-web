"""Strategy plug-in interface.

Strategies are pure signal generators: they see market context, never executors,
never the risk engine. `generate_signal` runs at closed-bar boundaries only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.models.enums import Direction, Regime
from app.models.market import Candle
from app.models.signals import EvidenceItem, Signal
from app.regimes.classifier import RegimeResult


@dataclass
class StrategyContext:
    """Everything a strategy may look at. Built identically in live, replay and backtest."""

    symbol: str
    timeframe: str
    now: datetime
    candles: list[Candle]              # closed bars only, last = most recent closed
    features: dict[str, float]
    regime: RegimeResult
    micro: dict[str, float] = field(default_factory=dict)
    event_evidence: float = 0.0        # -1..1 external evidence (decayed, confirmed)
    data_quality: float = 1.0

    @property
    def close(self) -> float:
        return self.candles[-1].close


@dataclass
class OpenPositionView:
    """Read-only view of an open position for exit logic."""

    direction: Direction
    entry_price: float
    entry_time: datetime
    stop: float | None
    target: float | None


class BaseStrategy(ABC):
    name: str = "base"
    version: str = "1.0"
    required_features: tuple[str, ...] = ()
    eligible_regimes: frozenset[Regime] = frozenset()
    risk_profile: dict = {}
    params: dict

    def __init__(self, **params) -> None:
        self.params = {**self.default_params(), **params}

    @classmethod
    def default_params(cls) -> dict:
        return {}

    @abstractmethod
    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        """Return an entry Signal or None. Must not mutate ctx."""

    def should_exit(self, ctx: StrategyContext, position: OpenPositionView) -> str | None:
        """Return an exit reason (string) or None to hold. Stops/targets are handled
        by the execution layer; this covers structure-based exits."""
        return None

    def has_required_features(self, features: dict[str, float]) -> bool:
        import math

        for name in self.required_features:
            value = features.get(name)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return False
        return True

    def _base_signal(self, ctx: StrategyContext, direction: Direction, *, confidence: float,
                     stop: float | None, target: float | None, evidence: list[EvidenceItem],
                     risks: list[str], invalidation: float | None = None,
                     expected_holding_h: float | None = None) -> Signal:
        return Signal(
            symbol=ctx.symbol,
            strategy=self.name,
            strategy_version=self.version,
            direction=direction,
            timestamp=ctx.now,
            timeframe=ctx.timeframe,
            reference_price=ctx.close,
            confidence=confidence,
            invalidation_level=invalidation if invalidation is not None else stop,
            hypothetical_stop=stop,
            hypothetical_target=target,
            expected_holding_period_hours=expected_holding_h,
            market_regime=ctx.regime.regime,
            evidence=evidence,
            risks=risks,
            data_quality=ctx.data_quality,
            external_confirmation=ctx.event_evidence,
            features_used={k: ctx.features[k] for k in self.required_features if k in ctx.features},
        )
