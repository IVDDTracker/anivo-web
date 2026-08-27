"""Event-driven backtest engine.

Anti-lookahead construction:
- the strategy sees only candles[0..i] (closed bars) at decision time i;
- every action (entry, strategy exit) executes at bar i+1's OPEN plus slippage;
- stops/targets are evaluated against bar i+1's range with conservative
  assumptions: gap-through fills at the open, and if stop AND target are both
  touched in the same bar the STOP is assumed to fill first.

Costs are always on: taker fees + base slippage (bps). Exchange filters
(minNotional / stepSize) are applied to sizing when SymbolRules are provided.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.config.settings import CostConfig, RegimeConfig
from app.features.engine import MIN_BARS, compute_features
from app.models.enums import Direction
from app.models.market import Candle, SymbolRules
from app.regimes.classifier import RegimeResult, classify
from app.strategies.base import BaseStrategy, OpenPositionView, StrategyContext


@dataclass
class BtTrade:
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    qty: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    exit_reason: str = ""
    signal_confidence: float = 0.0

    @property
    def holding_hours(self) -> float:
        if self.exit_time is None:
            return 0.0
        return (self.exit_time - self.entry_time).total_seconds() / 3600.0

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction, "entry_time": self.entry_time,
            "exit_time": self.exit_time, "entry_price": self.entry_price,
            "exit_price": self.exit_price, "qty": self.qty, "pnl": self.pnl, "fees": self.fees,
            "slippage": self.slippage, "exit_reason": self.exit_reason,
        }


@dataclass
class BacktestResult:
    backtest_id: str
    strategy: str
    strategy_version: str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    params: dict
    trades: list[BtTrade]
    equity_curve: list[tuple[datetime, float]]
    metrics: dict = field(default_factory=dict)
    signals_generated: int = 0
    signals_skipped_regime: int = 0


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_pct_per_trade: float = 1.0
    max_notional_pct: float = 20.0
    costs: CostConfig = field(default_factory=CostConfig)
    regime_cfg: RegimeConfig = field(default_factory=RegimeConfig)
    warmup_bars: int = 210
    apply_regime_filter: bool = True


class Backtester:
    def __init__(self, config: BacktestConfig | None = None,
                 rules: SymbolRules | None = None) -> None:
        self.cfg = config or BacktestConfig()
        self.rules = rules

    # ── cost helpers ─────────────────────────────────────────────────────────

    def _buy_fill(self, ref_price: float) -> tuple[float, float]:
        slip = ref_price * self.cfg.costs.base_slippage_bps / 10_000.0
        return ref_price + slip, slip

    def _sell_fill(self, ref_price: float) -> tuple[float, float]:
        slip = ref_price * self.cfg.costs.base_slippage_bps / 10_000.0
        return ref_price - slip, slip

    def _fee(self, notional: float) -> float:
        return notional * self.cfg.costs.taker_fee_bps / 10_000.0

    def _size(self, equity: float, entry: float, stop: float | None) -> float:
        max_notional = equity * self.cfg.max_notional_pct / 100.0
        if stop is not None and entry > stop > 0:
            risk_capital = equity * self.cfg.risk_pct_per_trade / 100.0
            qty = risk_capital / (entry - stop)
        else:
            qty = max_notional / entry
        qty = min(qty, max_notional / entry)
        if self.rules is not None:
            qty = float(self.rules.quantize_qty(Decimal(str(qty))))
            if self.rules.min_notional > 0 and Decimal(str(entry)) * Decimal(str(qty)) < self.rules.min_notional:
                return 0.0
        return max(qty, 0.0)

    # ── main loop ────────────────────────────────────────────────────────────

    def run(self, strategy: BaseStrategy, candles: list[Candle]) -> BacktestResult:
        cfg = self.cfg
        n = len(candles)
        warmup = max(cfg.warmup_bars, MIN_BARS)
        result = BacktestResult(
            backtest_id=str(uuid.uuid4()), strategy=strategy.name,
            strategy_version=strategy.version,
            symbol=candles[0].symbol if candles else "?",
            timeframe=candles[0].timeframe if candles else "?",
            start=candles[0].open_time if candles else datetime.min,
            end=candles[-1].close_time if candles else datetime.min,
            params=dict(strategy.params), trades=[], equity_curve=[],
        )
        if n <= warmup + 1:
            return result

        cash = cfg.initial_equity
        position: BtTrade | None = None
        pos_stop: float | None = None
        pos_target: float | None = None

        for i in range(warmup, n - 1):
            history = candles[: i + 1]
            bar = candles[i]
            next_bar = candles[i + 1]
            features = compute_features(history)
            regime: RegimeResult = classify(history, features, cfg.regime_cfg)
            ctx = StrategyContext(
                symbol=bar.symbol, timeframe=bar.timeframe, now=bar.close_time,
                candles=history, features=features, regime=regime,
            )

            if position is not None:
                exit_fee = self._process_exits(strategy, ctx, position, pos_stop, pos_target, next_bar)
                if exit_fee is not None:
                    cash += position.qty * (position.exit_price or 0.0) - exit_fee
                    position = None
                    pos_stop = pos_target = None
            elif features:
                signal = strategy.generate_signal(ctx)
                if signal is not None and signal.direction == Direction.LONG:
                    result.signals_generated += 1
                    if cfg.apply_regime_filter and strategy.eligible_regimes and \
                            regime.regime not in strategy.eligible_regimes:
                        result.signals_skipped_regime += 1
                    else:
                        equity_now = cash
                        fill_price, slip = self._buy_fill(next_bar.open)
                        qty = self._size(equity_now, fill_price, signal.hypothetical_stop)
                        notional = qty * fill_price
                        if qty > 0 and notional <= cash:
                            fee = self._fee(notional)
                            cash -= notional + fee
                            position = BtTrade(
                                symbol=bar.symbol, direction="LONG",
                                entry_time=next_bar.open_time, entry_price=fill_price,
                                qty=qty, fees=fee, slippage=slip * qty,
                                signal_confidence=signal.confidence,
                            )
                            pos_stop = signal.hypothetical_stop
                            pos_target = signal.hypothetical_target
                            result.trades.append(position)

            mark = next_bar.close
            equity = cash + (position.qty * mark if position is not None else 0.0)
            result.equity_curve.append((next_bar.close_time, equity))

        # liquidate any open position at the last close (accounting completeness)
        if position is not None:
            last = candles[-1]
            fill, slip = self._sell_fill(last.close)
            fee = self._fee(position.qty * fill)
            position.exit_time = last.close_time
            position.exit_price = fill
            position.fees += fee
            position.slippage += slip * position.qty
            position.pnl = (fill - position.entry_price) * position.qty - position.fees
            position.exit_reason = "end_of_data"
            cash += position.qty * fill - fee
            result.equity_curve[-1] = (last.close_time, cash)

        return result

    def _process_exits(self, strategy: BaseStrategy, ctx: StrategyContext, position: BtTrade,
                       stop: float | None, target: float | None, next_bar: Candle) -> float | None:
        """Evaluate stop/target on next bar's range, then structure exits at next open.
        Returns the exit fee when the position closed, None when it stays open.
        Conservative: stop before target when both are touched in the same bar."""
        exit_price: float | None = None
        reason = ""
        if stop is not None and next_bar.low <= stop:
            raw = next_bar.open if next_bar.open <= stop else stop  # gap-through fills at open
            exit_price, _ = self._sell_fill(raw)
            reason = "stop"
        elif target is not None and next_bar.high >= target:
            raw = next_bar.open if next_bar.open >= target else target
            exit_price, _ = self._sell_fill(raw)
            reason = "target"
        else:
            structure_reason = strategy.should_exit(
                ctx,
                OpenPositionView(direction=Direction.LONG, entry_price=position.entry_price,
                                 entry_time=position.entry_time, stop=stop, target=target),
            )
            if structure_reason:
                exit_price, _ = self._sell_fill(next_bar.open)
                reason = structure_reason

        if exit_price is None:
            return None
        fee = self._fee(position.qty * exit_price)
        slip = abs(exit_price) * self.cfg.costs.base_slippage_bps / 10_000.0 * position.qty
        position.exit_time = next_bar.open_time
        position.exit_price = exit_price
        position.fees += fee
        position.slippage += slip
        position.pnl = (exit_price - position.entry_price) * position.qty - position.fees
        position.exit_reason = reason
        return fee
