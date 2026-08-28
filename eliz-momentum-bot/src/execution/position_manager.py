"""Position manager: open/close legs from fills, PnL accounting, restart reconcile.

Futures PnL (linear USDT contracts):
  LONG leg:  (exit − entry) × qty − fees
  SHORT leg: (entry − exit) × qty − fees

Restart reconcile (spec §20): open positions in the DB are compared with the
exchange (live) — an exchange position we don't know, or a DB position the
exchange doesn't have, trips the kill switch instead of guessing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from src.core.clock import Clock
from src.core.domain import OrderResult, PositionRecord, PositionSide
from src.core.logger import get_logger
from src.risk.kill_switch import KillSwitch
from src.storage.database import Repo

log = get_logger(__name__)


class PositionManager:
    def __init__(self, repo: Repo, clock: Clock, kill: KillSwitch) -> None:
        self.repo = repo
        self.clock = clock
        self.kill = kill
        self.open: dict[str, PositionRecord] = {}  # symbol → open position

    async def open_leg(self, *, session_id: str, tweet_id: str, symbol: str,
                       side: PositionSide, fill: OrderResult) -> PositionRecord:
        if fill.executed_price is None or fill.executed_qty <= 0:
            raise ValueError("cannot open leg without an executed fill")
        pos = PositionRecord(
            session_id=session_id, symbol=symbol, side=side, qty=fill.executed_qty,
            entry_price=Decimal(str(fill.executed_price)), opened_at=self.clock.now(),
            fees=fill.fee_usdt)
        self.open[symbol] = pos
        await self.repo.store_position(pos)
        await self.repo.store_trade(
            id=pos.id, session_id=session_id, tweet_id=tweet_id, symbol=symbol,
            leg=side.value, entry_price=float(pos.entry_price), qty=float(pos.qty),
            notional_usdt=float(pos.entry_price * pos.qty), fees=float(fill.fee_usdt),
            slippage_cost=float(pos.entry_price * pos.qty)
            * (fill.slippage_pct or 0.0) / 100.0,
            opened_at=pos.opened_at)
        return pos

    async def close_leg(self, pos: PositionRecord, fill: OrderResult, *, reason: str,
                        peak_price: float | None = None,
                        reversal_score: float | None = None) -> float:
        """Returns NET pnl of the leg."""
        if fill.executed_price is None:
            raise ValueError("cannot close leg without an executed fill")
        exit_price = Decimal(str(fill.executed_price))
        sign = Decimal("1") if pos.side == PositionSide.LONG else Decimal("-1")
        gross = sign * (exit_price - pos.entry_price) * pos.qty
        total_fees = pos.fees + fill.fee_usdt
        net = gross - total_fees
        pos.closed_at = self.clock.now()
        pos.exit_price = exit_price
        pos.realized_pnl = net
        pos.fees = total_fees
        pos.close_reason = reason
        self.open.pop(pos.symbol, None)
        await self.repo.update_position(pos)
        await self.repo.update_trade(
            pos.id, exit_price=float(exit_price), gross_pnl=float(gross),
            fees=float(total_fees), net_pnl=float(net), closed_at=pos.closed_at,
            close_reason=reason, peak_price=peak_price,
            reversal_score_at_exit=reversal_score)
        return float(net)

    # ── restart safety ───────────────────────────────────────────────────────

    async def restore_and_reconcile(self, now: datetime, *, live_client=None) -> None:
        rows = await self.repo.open_positions()
        for row in rows:
            self.open[row.symbol] = PositionRecord(
                id=row.id, session_id=row.session_id, symbol=row.symbol,
                side=PositionSide(row.side), qty=row.qty, entry_price=row.entry_price,
                opened_at=row.opened_at, fees=row.fees)
        if live_client is None:
            if self.open:
                log.warning("restored %d open paper positions", len(self.open))
            return
        try:
            exchange_positions = {
                p["symbol"]: float(p.get("positionAmt", 0.0))
                for p in await live_client.position_risk()
                if abs(float(p.get("positionAmt", 0.0))) > 0}
        except Exception as exc:
            self.kill.trip("EXCHANGE_API_PROBLEM", f"positionRisk failed: {str(exc)[:150]}")
            return
        for symbol, amt in exchange_positions.items():
            if symbol not in self.open:
                self.kill.trip("UNEXPECTED_OPEN_POSITION",
                               f"exchange has {amt} {symbol} we don't know about")
        for symbol, pos in list(self.open.items()):
            if symbol not in exchange_positions:
                log.warning("DB position %s missing on exchange — closing locally as "
                            "reconcile_lost", symbol)
                pos.closed_at = now
                pos.close_reason = "reconcile_lost_on_exchange"
                await self.repo.update_position(pos)
                self.open.pop(symbol, None)
                self.kill.trip("ORDER_STATE_UNCERTAIN",
                               f"{symbol} position vanished; manual review required")

    def dedup_id(self) -> str:
        return str(uuid.uuid4())
