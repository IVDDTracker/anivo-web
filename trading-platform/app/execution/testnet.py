"""Binance Spot Testnet executor.

Order safety protocol (DECISIONS.md D-013):
1. intent persisted → 2. order row persisted as PENDING_SUBMIT with a
DETERMINISTIC clientOrderId derived from the intent id → 3. send. On
timeout/5xx/-1007 the order becomes UNKNOWN and is only resolved by the
reconciler querying `origClientOrderId` — a resend (manual or automatic) reuses
the SAME clientOrderId so the exchange rejects duplicates instead of
double-executing. exchangeInfo filters (tickSize/stepSize/minNotional) are
enforced before submission.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.bus import EventBus, Topics
from app.core.clock import Clock
from app.core.errors import ExchangeError, FilterViolation, OrderOutcomeUnknown
from app.core.hashing import deterministic_client_order_id
from app.core.logging import get_logger
from app.execution.binance_client import BinanceSignedClient
from app.models.enums import Direction, OrderSide, OrderStatus, OrderType, Venue
from app.models.market import SymbolRules
from app.models.orders import Fill, Order, Position, TradeIntent
from app.storage.repositories import OrderRepository

log = get_logger(__name__)

_STATUS_MAP = {
    "NEW": OrderStatus.SUBMITTED,
    "PENDING_NEW": OrderStatus.SUBMITTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.SUBMITTED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}


def map_exchange_status(raw: str) -> OrderStatus:
    return _STATUS_MAP.get(raw, OrderStatus.UNKNOWN)


class TestnetExecutor:
    def __init__(self, client: BinanceSignedClient, repo: OrderRepository, clock: Clock,
                 *, rules: dict[str, SymbolRules], bus: EventBus | None = None) -> None:
        if not client.is_testnet:
            # defense in depth — the transport would refuse anyway (layer 2)
            from app.core.errors import ProductionExecutionDisabled

            raise ProductionExecutionDisabled(
                "TestnetExecutor must be constructed with a testnet client")
        self.client = client
        self.repo = repo
        self.clock = clock
        self.rules = rules
        self.bus = bus

    # ── order submission ─────────────────────────────────────────────────────

    def _prepare(self, intent: TradeIntent) -> tuple[Decimal, Decimal | None]:
        rules = self.rules.get(intent.symbol)
        if rules is None:
            raise FilterViolation(f"no exchange rules loaded for {intent.symbol}")
        qty = rules.quantize_qty(intent.quantity)
        price: Decimal | None = None
        if intent.order_type == OrderType.LIMIT:
            if intent.limit_price is None:
                raise FilterViolation("limit order without limit price")
            price = rules.quantize_price(intent.limit_price)
        check_price = price if price is not None else rules.quantize_price(intent.reference_price)
        problems = rules.validate_order(check_price, qty,
                                        is_market=intent.order_type == OrderType.MARKET)
        if problems:
            raise FilterViolation("; ".join(problems))
        return qty, price

    async def submit(self, intent: TradeIntent) -> Order:
        qty, price = self._prepare(intent)  # raises before anything is persisted
        now = self.clock.now()
        client_order_id = deterministic_client_order_id(intent.id)
        await self.repo.store_intent(intent)
        order = Order(
            intent_id=intent.id, client_order_id=client_order_id, venue=Venue.TESTNET,
            symbol=intent.symbol, side=intent.side, order_type=intent.order_type,
            time_in_force=intent.time_in_force, quantity=qty, price=price,
            status=OrderStatus.PENDING_SUBMIT, strategy=intent.strategy,
            created_at=now, updated_at=now,
        )
        await self.repo.store_order(order)  # persisted BEFORE sending
        return await self._send(order)

    async def _send(self, order: Order) -> Order:
        try:
            resp = await self.client.new_order(
                symbol=order.symbol, side=order.side.value, order_type=order.order_type.value,
                quantity=str(order.quantity),
                price=str(order.price) if order.price is not None else None,
                time_in_force=(order.time_in_force.value
                               if order.order_type == OrderType.LIMIT else None),
                client_order_id=order.client_order_id,
            )
        except OrderOutcomeUnknown as exc:
            order.status = OrderStatus.UNKNOWN
            order.error = str(exc)[:400]
            order.updated_at = self.clock.now()
            await self.repo.update_order(order)
            log.warning("order outcome UNKNOWN for %s — reconciliation required",
                        order.client_order_id)
            return order
        except ExchangeError as exc:
            if exc.code == -2010 and "duplicate" in str(exc).lower():
                # our idempotency working as intended: the order already exists
                log.info("duplicate clientOrderId %s — reconciling instead of resending",
                         order.client_order_id)
                order.status = OrderStatus.UNKNOWN
                order.error = "duplicate clientOrderId; reconcile"
                await self.repo.update_order(order)
                return order
            order.status = OrderStatus.REJECTED
            order.error = str(exc)[:400]
            order.updated_at = self.clock.now()
            await self.repo.update_order(order)
            await self._publish(order)
            return order

        await self.apply_exchange_state(order, resp)
        return order

    async def resubmit_unknown(self, order: Order) -> Order:
        """Resolve an UNKNOWN order: query first; resend (same id) ONLY if absent."""
        if order.status != OrderStatus.UNKNOWN:
            return order
        remote = await self.client.get_order(
            symbol=order.symbol, orig_client_order_id=order.client_order_id)
        if remote is not None:
            await self.apply_exchange_state(order, remote)
            return order
        log.info("order %s not on exchange; resending with SAME clientOrderId",
                 order.client_order_id)
        return await self._send(order)

    async def cancel(self, order: Order) -> Order:
        try:
            resp = await self.client.cancel_order(
                symbol=order.symbol, orig_client_order_id=order.client_order_id)
        except OrderOutcomeUnknown:
            order.status = OrderStatus.UNKNOWN
            order.error = "cancel outcome unknown"
            await self.repo.update_order(order)
            return order
        except ExchangeError as exc:
            if exc.code == -2013:  # already gone
                order.status = OrderStatus.CANCELED
                await self.repo.update_order(order)
                return order
            raise
        await self.apply_exchange_state(order, resp)
        return order

    # ── state sync ───────────────────────────────────────────────────────────

    async def apply_exchange_state(self, order: Order, resp: dict) -> None:
        """Update local order (and positions on fill progress) from an exchange payload."""
        prev_filled = order.filled_qty
        order.exchange_order_id = str(resp.get("orderId", order.exchange_order_id or ""))
        order.status = map_exchange_status(str(resp.get("status", "")))
        executed = Decimal(str(resp.get("executedQty", "0")))
        cummulative_quote = Decimal(str(resp.get("cummulativeQuoteQty", "0")))
        order.filled_qty = executed
        if executed > 0 and cummulative_quote > 0:
            order.avg_fill_price = cummulative_quote / executed
        order.updated_at = self.clock.now()
        await self.repo.update_order(order)
        await self._publish(order)

        new_qty = executed - prev_filled
        if new_qty > 0 and order.avg_fill_price is not None:
            fee = sum((Decimal(str(f.get("commission", "0"))) for f in resp.get("fills", [])),
                      Decimal("0"))
            fill = Fill(order_id=order.id, venue=Venue.TESTNET, symbol=order.symbol,
                        side=order.side, price=order.avg_fill_price, qty=new_qty, fee=fee,
                        timestamp=self.clock.now())
            await self.repo.store_fill(fill)
            await self._apply_fill_to_position(order, fill)

    async def _apply_fill_to_position(self, order: Order, fill: Fill) -> None:
        open_positions = {p.symbol: p for p in await self.repo.open_positions()}
        pos = open_positions.get(order.symbol)
        intent_stop = intent_target = None
        now = self.clock.now()
        if order.side == OrderSide.BUY:
            if pos is None:
                pos = Position(
                    venue=Venue.TESTNET, symbol=order.symbol, direction=Direction.LONG,
                    qty=fill.qty, avg_entry_price=fill.price, stop_price=intent_stop,
                    target_price=intent_target, strategy=order.strategy,
                    opened_at=now, fees_paid=fill.fee,
                )
                await self.repo.store_position(pos)
            else:
                total = pos.qty + fill.qty
                pos.avg_entry_price = (pos.avg_entry_price * pos.qty + fill.price * fill.qty) / total
                pos.qty = total
                pos.fees_paid += fill.fee
                await self.repo.update_position(pos)
        else:
            if pos is None:
                log.warning("testnet sell fill without local position on %s", order.symbol)
                return
            pnl = (fill.price - pos.avg_entry_price) * fill.qty - fill.fee
            pos.realized_pnl += pnl
            pos.fees_paid += fill.fee
            pos.qty -= fill.qty
            if pos.qty <= 0:
                pos.closed_at = now
                pos.close_reason = order.error or "exit"
            await self.repo.update_position(pos)
        if self.bus is not None:
            await self.bus.publish(Topics.POSITION_UPDATE, pos)

    async def _publish(self, order: Order) -> None:
        if self.bus is not None:
            await self.bus.publish(Topics.ORDER_UPDATE, order)


class TestnetReconciler:
    """Periodically reconciles local order state against the exchange.

    Fail-safe: mismatches degrade the `testnet` component (blocking NEW testnet
    entries) until state converges. A full testnet data reset (Binance wipes
    testnet periodically) is detected as known-but-vanished orders and closes
    local open state with reason TESTNET_RESET.
    """

    def __init__(self, executor: TestnetExecutor, repo: OrderRepository, clock: Clock,
                 *, state=None) -> None:
        self.executor = executor
        self.repo = repo
        self.clock = clock
        self.state = state

    async def reconcile_once(self) -> dict[str, int]:
        stats = {"checked": 0, "resolved_unknown": 0, "synced": 0, "vanished": 0}
        pending = await self.repo.orders_with_status([
            OrderStatus.PENDING_SUBMIT.value, OrderStatus.UNKNOWN.value,
            OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value,
        ])
        mismatch = False
        for order in pending:
            stats["checked"] += 1
            try:
                if order.status in (OrderStatus.UNKNOWN, OrderStatus.PENDING_SUBMIT):
                    remote = await self.executor.client.get_order(
                        symbol=order.symbol, orig_client_order_id=order.client_order_id)
                    if remote is None:
                        # never sent (crash between persist and send) or wiped by
                        # a testnet reset. Fail-safe: mark rejected, DO NOT resend
                        # automatically — a human or the pipeline may re-decide.
                        order.status = OrderStatus.REJECTED
                        order.error = "not found on exchange during reconcile"
                        order.updated_at = self.clock.now()
                        await self.repo.update_order(order)
                        stats["vanished"] += 1
                    else:
                        await self.executor.apply_exchange_state(order, remote)
                        stats["resolved_unknown"] += 1
                else:
                    remote = await self.executor.client.get_order(
                        symbol=order.symbol, orig_client_order_id=order.client_order_id)
                    if remote is None:
                        order.status = OrderStatus.REJECTED
                        order.error = "open order vanished from exchange (testnet reset?)"
                        order.updated_at = self.clock.now()
                        await self.repo.update_order(order)
                        stats["vanished"] += 1
                        mismatch = True
                    else:
                        await self.executor.apply_exchange_state(order, remote)
                        stats["synced"] += 1
            except (ExchangeError, OrderOutcomeUnknown) as exc:
                log.warning("reconcile failed for %s: %s", order.client_order_id, exc)
                mismatch = True
        if self.state is not None:
            self.state.set_component_degraded("testnet_reconcile", mismatch)
        return stats
