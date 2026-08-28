"""Risk manager (spec §10): sizing + hard limits for ~500 USDT capital.

- risk-based sizing: qty ≈ MAX_RISK_PER_TRADE_USDT / stop-distance, capped by
  MAX_POSITION_NOTIONAL_USDT and MAX_LEVERAGE, floored to exchange stepSize;
- daily loss / trade count / consecutive-loss limits latch the kill switch;
- limits are restored from daily_stats at startup so a restart cannot reset
  a burned daily budget. NO position ever risks the whole account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from src.core.config import Settings
from src.core.domain import SkipReason
from src.core.logger import get_logger
from src.exchange.symbol_mapper import SymbolRules
from src.risk.kill_switch import KillSwitch
from src.storage.database import Repo

log = get_logger(__name__)


class SizingResult(BaseModel):
    approved: bool
    quantity: Decimal = Decimal("0")
    notional_usdt: float = 0.0
    est_max_loss_usdt: float = 0.0
    leverage: int = 1
    skip_reason: SkipReason | None = None
    detail: str = ""


@dataclass
class RiskManager:
    cfg: Settings
    repo: Repo
    kill: KillSwitch

    trades_today: int = 0
    realized_pnl_today: float = 0.0
    consecutive_losses: int = 0
    _day: str = ""

    # ── persistence-backed daily state ───────────────────────────────────────

    async def restore(self, now: datetime) -> None:
        self._day = now.date().isoformat()
        row = await self.repo.get_daily(self._day)
        if row is not None:
            self.trades_today = row.trades
            self.realized_pnl_today = row.realized_pnl
            self.consecutive_losses = row.consecutive_losses
            if row.kill_switch_fired:
                self.kill.trip("DAILY_LOSS_LIMIT", "restored from daily_stats after restart")
        self._enforce_daily_limits()

    def _roll_day(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day != self._day:
            self._day = day
            self.trades_today = 0
            self.realized_pnl_today = 0.0
            self.kill.clear("DAILY_LOSS_LIMIT")
            self.kill.clear("MAX_TRADES_PER_DAY")

    def _enforce_daily_limits(self) -> None:
        if -self.realized_pnl_today >= self.cfg.max_daily_loss_usdt:
            self.kill.trip("DAILY_LOSS_LIMIT",
                           f"daily pnl {self.realized_pnl_today:+.2f} USDT ≤ "
                           f"-{self.cfg.max_daily_loss_usdt}")
        if self.trades_today >= self.cfg.max_trades_per_day:
            self.kill.trip("MAX_TRADES_PER_DAY", f"{self.trades_today} trades today")
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.kill.trip("CONSECUTIVE_LOSSES", f"{self.consecutive_losses} in a row")

    async def on_trade_opened(self, now: datetime) -> None:
        self._roll_day(now)
        self.trades_today += 1
        await self.repo.upsert_daily(self._day, trades_delta=1, now=now)
        self._enforce_daily_limits()

    async def on_leg_closed(self, net_pnl: float, fees: float, now: datetime) -> None:
        self._roll_day(now)
        self.realized_pnl_today += net_pnl
        self.consecutive_losses = self.consecutive_losses + 1 if net_pnl < 0 else 0
        self._enforce_daily_limits()
        await self.repo.upsert_daily(
            self._day, pnl_delta=net_pnl, fees_delta=fees,
            consecutive_losses=self.consecutive_losses,
            kill_switch=self.kill.active, now=now)
        if self.kill.active:
            log.warning("kill switch active after close: %s", self.kill.reasons)

    # ── sizing ───────────────────────────────────────────────────────────────

    def size_entry(self, *, price: float, stop_price: float, rules: SymbolRules,
                   now: datetime) -> SizingResult:
        self._roll_day(now)
        self._enforce_daily_limits()
        if self.kill.active:
            return SizingResult(approved=False, skip_reason=SkipReason.KILL_SWITCH,
                                detail=str(self.kill.reasons))
        if price <= 0 or stop_price <= 0 or stop_price >= price:
            return SizingResult(approved=False, skip_reason=SkipReason.RISK_LIMIT,
                                detail="invalid price/stop for LONG sizing")

        stop_distance = price - stop_price
        qty_by_risk = self.cfg.max_risk_per_trade_usdt / stop_distance
        max_notional = min(self.cfg.max_position_notional_usdt,
                           self.cfg.account_capital * self.cfg.max_leverage)
        qty_by_notional = max_notional / price
        qty = Decimal(str(min(qty_by_risk, qty_by_notional)))
        qty = rules.quantize_qty(qty)
        if qty <= 0:
            return SizingResult(approved=False, skip_reason=SkipReason.RISK_LIMIT,
                                detail="sized to zero after exchange stepSize")
        notional = float(qty) * price
        if rules.min_notional > 0 and Decimal(str(price)) * qty < rules.min_notional:
            return SizingResult(approved=False, skip_reason=SkipReason.RISK_LIMIT,
                                detail=f"below exchange minNotional {rules.min_notional}")
        est_loss = float(qty) * stop_distance
        if est_loss > self.cfg.max_risk_per_trade_usdt * 1.5:
            return SizingResult(approved=False, skip_reason=SkipReason.RISK_LIMIT,
                                detail=f"est loss {est_loss:.2f} exceeds per-trade budget")
        leverage = max(1, min(self.cfg.max_leverage,
                              int(notional / self.cfg.account_capital) + 1))
        return SizingResult(approved=True, quantity=qty, notional_usdt=round(notional, 2),
                            est_max_loss_usdt=round(est_loss, 2), leverage=leverage,
                            detail=f"risk {est_loss:.2f} USDT over "
                                   f"{stop_distance / price * 100:.2f}% stop")

    def snapshot(self) -> dict:
        return {"day": self._day, "trades_today": self.trades_today,
                "realized_pnl_today": round(self.realized_pnl_today, 2),
                "consecutive_losses": self.consecutive_losses,
                "kill_switch": self.kill.reasons,
                "limits": {
                    "max_risk_per_trade_usdt": self.cfg.max_risk_per_trade_usdt,
                    "max_daily_loss_usdt": self.cfg.max_daily_loss_usdt,
                    "max_position_notional_usdt": self.cfg.max_position_notional_usdt,
                    "max_leverage": self.cfg.max_leverage,
                    "max_trades_per_day": self.cfg.max_trades_per_day,
                    "max_consecutive_losses": self.cfg.max_consecutive_losses,
                }}
