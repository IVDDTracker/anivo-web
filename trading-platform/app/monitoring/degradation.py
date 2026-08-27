"""Automatic strategy degradation detection.

Monitors rolling live (paper/testnet) results per strategy. On breach: the
strategy is marked DEGRADED (no new testnet entries; paper observation
continues) and Telegram is alerted. It never re-tunes or redesigns a strategy —
recovery is a human research decision (STRATEGY_RESEARCH.md).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.logging import get_logger
from app.models.enums import StrategyStage
from app.models.orders import Position
from app.strategies.registry import StrategyRegistry

log = get_logger(__name__)


@dataclass
class DegradationConfig:
    window_trades: int = 30
    min_trades_to_judge: int = 10
    max_losing_streak: int = 7
    min_expectancy: float = 0.0          # rolling expectancy must stay above this
    max_rolling_drawdown_pct: float = 10.0
    expected_win_rate: float | None = None  # optional backtest anchor
    win_rate_tolerance: float = 0.25


@dataclass
class _StrategyStats:
    pnls: deque = field(default_factory=lambda: deque(maxlen=200))
    streak: int = 0
    equity: float = 0.0
    peak: float = 0.0


@dataclass
class DegradationDetector:
    registry: StrategyRegistry
    clock: Clock
    cfg: DegradationConfig = field(default_factory=DegradationConfig)
    notifier: object = None
    _stats: dict[str, _StrategyStats] = field(default_factory=dict)

    async def on_position_closed(self, position: Position) -> list[str]:
        """Feed a closed position; returns list of breach descriptions (possibly empty)."""
        name = position.strategy
        if not name:
            return []
        stats = self._stats.setdefault(name, _StrategyStats())
        pnl = float(position.realized_pnl)
        stats.pnls.append(pnl)
        stats.streak = stats.streak + 1 if pnl < 0 else 0
        stats.equity += pnl
        stats.peak = max(stats.peak, stats.equity)

        breaches = self._evaluate(name, stats)
        if breaches:
            await self._degrade(name, breaches)
        return breaches

    def _evaluate(self, name: str, stats: _StrategyStats) -> list[str]:
        cfg = self.cfg
        breaches: list[str] = []
        recent = list(stats.pnls)[-cfg.window_trades:]
        if stats.streak >= cfg.max_losing_streak:
            breaches.append(f"losing streak {stats.streak} ≥ {cfg.max_losing_streak}")
        if len(recent) >= cfg.min_trades_to_judge:
            expectancy = sum(recent) / len(recent)
            if expectancy < cfg.min_expectancy:
                breaches.append(
                    f"rolling expectancy {expectancy:.4f} < {cfg.min_expectancy} "
                    f"over {len(recent)} trades")
            if cfg.expected_win_rate is not None:
                win_rate = sum(1 for p in recent if p > 0) / len(recent)
                if win_rate < cfg.expected_win_rate - cfg.win_rate_tolerance:
                    breaches.append(
                        f"win rate {win_rate:.2f} far below expected "
                        f"{cfg.expected_win_rate:.2f}")
        if stats.peak > 0:
            dd_abs = stats.peak - stats.equity
            # relative to a notional base equal to peak PnL + guard
            if dd_abs > 0 and stats.peak > 0:
                dd_pct = dd_abs / max(stats.peak, 1e-9) * 100
                if dd_pct >= cfg.max_rolling_drawdown_pct and dd_abs > abs(
                        sum(stats.pnls) / max(len(stats.pnls), 1)) * 5:
                    breaches.append(f"strategy PnL drawdown {dd_pct:.0f}% from peak")
        return breaches

    async def _degrade(self, name: str, breaches: list[str]) -> None:
        record = self.registry.records.get(name)
        if record is None or record.stage == StrategyStage.DEGRADED:
            return
        detail = "; ".join(breaches)
        await self.registry.set_stage(name, StrategyStage.DEGRADED, detail)
        log.warning("strategy %s DEGRADED: %s", name, detail)
        if self.notifier is not None:
            self.notifier.strategy_degraded(name, detail)

    def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "trades_tracked": len(stats.pnls),
                "losing_streak": stats.streak,
                "net_pnl": round(stats.equity, 2),
                "rolling_expectancy": (round(sum(list(stats.pnls)[-self.cfg.window_trades:])
                                             / max(len(list(stats.pnls)
                                                       [-self.cfg.window_trades:]), 1), 4)),
            }
            for name, stats in self._stats.items()
        }
