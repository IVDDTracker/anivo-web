"""Execution adapters (spec §13): identical strategy code, swappable execution.

- PaperAdapter: fills against the LIVE order book (bid/ask + configurable
  slippage + taker fee). No book → no fill (fail-safe, never fantasy fills).
- LiveAdapter: real Binance USDⓈ-M orders. Constructing it REQUIRES
  settings.live_execution_enabled (MODE=LIVE + ENABLE_LIVE_TRADING) — otherwise
  it raises immediately; the transport client re-checks on every mutating call.
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import Protocol

from src.core.clock import Clock
from src.core.config import Settings
from src.core.domain import BookTop, OrderIntent, OrderResult, OrderSide, OrderStatus
from src.core.logger import get_logger
from src.exchange.binance_client import (
    BinanceFuturesClient,
    ExchangeError,
    LiveTradingDisabled,
    OrderOutcomeUnknown,
)

log = get_logger(__name__)


def client_order_id(intent_id: str) -> str:
    """Deterministic id from the intent — a resend reuses the same id, so the
    exchange rejects duplicates instead of double-filling."""
    return "eb-" + hashlib.sha256(intent_id.encode()).hexdigest()[:24]


class ExecutionAdapter(Protocol):
    name: str

    async def execute(self, intent: OrderIntent, *, book: BookTop | None) -> OrderResult: ...


class PaperAdapter:
    name = "paper"

    def __init__(self, cfg: Settings, clock: Clock) -> None:
        self.cfg = cfg
        self.clock = clock

    async def execute(self, intent: OrderIntent, *, book: BookTop | None) -> OrderResult:
        coid = client_order_id(intent.id)
        if book is None or book.bid_price <= 0 or book.ask_price <= 0:
            return OrderResult(intent_id=intent.id, client_order_id=coid,
                               status=OrderStatus.REJECTED,
                               error="no order book available for paper fill (fail-safe)")
        slip = self.cfg.paper_slippage_bps / 10_000.0
        if intent.side == OrderSide.BUY:
            requested = book.ask_price
            executed = book.ask_price * (1.0 + slip)
        else:
            requested = book.bid_price
            executed = book.bid_price * (1.0 - slip)
        qty = intent.quantity
        fee = Decimal(str(executed)) * qty * Decimal(str(self.cfg.taker_fee_rate))
        return OrderResult(
            intent_id=intent.id, client_order_id=coid, exchange_order_id=None,
            status=OrderStatus.FILLED, requested_price=requested, executed_price=executed,
            executed_qty=qty, fee_usdt=fee,
            slippage_pct=abs(executed / requested - 1.0) * 100.0,
            order_latency_ms=0.0)


class LiveAdapter:
    name = "live"

    def __init__(self, cfg: Settings, clock: Clock,
                 client: BinanceFuturesClient | None = None) -> None:
        if not cfg.live_execution_enabled:
            raise LiveTradingDisabled(
                "LiveAdapter requires MODE=LIVE and ENABLE_LIVE_TRADING=true "
                "(spec §13 double-flag). Refusing to construct.")
        self.cfg = cfg
        self.clock = clock
        self.client = client or BinanceFuturesClient(
            cfg.fapi_url, api_key=cfg.binance_api_key, api_secret=cfg.binance_api_secret,
            allow_trading=True, recv_window_ms=cfg.recv_window_ms)

    async def execute(self, intent: OrderIntent, *, book: BookTop | None) -> OrderResult:
        coid = client_order_id(intent.id)
        requested = None
        if book is not None:
            requested = book.ask_price if intent.side == OrderSide.BUY else book.bid_price
        started = time.monotonic()
        try:
            resp = await self.client.new_order(
                symbol=intent.symbol, side=intent.side.value,
                order_type="MARKET" if intent.order_type == "MARKET" else "LIMIT",
                quantity=str(intent.quantity), client_order_id=coid,
                price=str(intent.limit_price) if intent.limit_price is not None else None,
                time_in_force="IOC" if intent.order_type != "MARKET" else None,
                reduce_only=intent.reduce_only)
        except OrderOutcomeUnknown as exc:
            return OrderResult(intent_id=intent.id, client_order_id=coid,
                               status=OrderStatus.UNKNOWN, requested_price=requested,
                               order_latency_ms=(time.monotonic() - started) * 1000,
                               error=str(exc)[:300])
        except ExchangeError as exc:
            if exc.code == -4015 or "duplicate" in str(exc).lower():
                # idempotency working: same clientOrderId already accepted
                return OrderResult(intent_id=intent.id, client_order_id=coid,
                                   status=OrderStatus.UNKNOWN, requested_price=requested,
                                   order_latency_ms=(time.monotonic() - started) * 1000,
                                   error="duplicate clientOrderId; reconcile")
            return OrderResult(intent_id=intent.id, client_order_id=coid,
                               status=OrderStatus.REJECTED, requested_price=requested,
                               order_latency_ms=(time.monotonic() - started) * 1000,
                               error=str(exc)[:300])
        return self._map_response(intent, coid, resp, requested,
                                  (time.monotonic() - started) * 1000)

    @staticmethod
    def _map_response(intent: OrderIntent, coid: str, resp: dict,
                      requested: float | None, latency_ms: float) -> OrderResult:
        status_map = {"NEW": OrderStatus.SUBMITTED, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                      "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
                      "EXPIRED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED}
        executed_qty = Decimal(str(resp.get("executedQty", "0")))
        avg_price = float(resp.get("avgPrice") or 0.0) or None
        cum_quote = Decimal(str(resp.get("cumQuote", "0")))
        # RESULT responses don't itemize commissions; estimate as taker on filled notional
        fee = cum_quote * Decimal("0.0005") if cum_quote > 0 else Decimal("0")
        slippage = (abs(avg_price / requested - 1.0) * 100.0
                    if avg_price and requested else None)
        return OrderResult(
            intent_id=intent.id, client_order_id=coid,
            exchange_order_id=str(resp.get("orderId", "")),
            status=status_map.get(str(resp.get("status")), OrderStatus.UNKNOWN),
            requested_price=requested, executed_price=avg_price,
            executed_qty=executed_qty, fee_usdt=fee, slippage_pct=slippage,
            order_latency_ms=latency_ms)
