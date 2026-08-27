"""Paper trading engine: realistic internal execution simulator.

Modeled effects:
- market orders cross the spread (buy at ask / sell at bid) plus base slippage bps;
- limit orders rest until touched, fill at the limit price with maker fees;
- latency: an order becomes active `latency_ms` after submission;
- partial fills: a tick fills at most the displayed top-of-book quantity;
- order expiration (GTC orders may carry expires_at);
- position stops/targets are monitored tick-by-tick (stop = market sell on breach);
- every order/fill/position/PnL is persisted (venue=PAPER) and survives restart.

The engine accepts ONLY TradeIntents that already passed the DecisionPipeline —
it never invents trades.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.settings import CostConfig
from app.core.bus import EventBus, Topics
from app.core.clock import Clock
from app.core.logging import get_logger
from app.models.enums import Direction, OrderSide, OrderStatus, OrderType, Venue
from app.models.market import BookTicker
from app.models.orders import Fill, Order, Position, TradeIntent
from app.storage.repositories import OrderRepository

log = get_logger(__name__)

D0 = Decimal("0")


@dataclass
class PaperEngine:
    costs: CostConfig
    clock: Clock
    repo: OrderRepository
    bus: EventBus | None = None
    starting_cash: Decimal = Decimal("10000")
    on_position_closed: Callable[[Position, float], Awaitable[None]] | None = None

    cash: Decimal = field(default=D0)
    positions: dict[str, Position] = field(default_factory=dict)  # open, by symbol
    open_orders: dict[str, Order] = field(default_factory=dict)
    _intents: dict[str, TradeIntent] = field(default_factory=dict)
    _last_ticker: dict[str, BookTicker] = field(default_factory=dict)
    _peak_equity: Decimal = field(default=D0)

    def __post_init__(self) -> None:
        if self.cash == D0:
            self.cash = self.starting_cash
        self._peak_equity = self.cash

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def restore(self) -> None:
        """Rebuild open state after restart. Cash = starting - open position cost + realized."""
        open_positions = await self.repo.open_positions()
        realized = Decimal("0")
        # NB: realized PnL of closed positions is already reflected via performance
        # snapshots; conservative reconstruction: cash = starting + sum(realized of all
        # closed) - sum(entry cost of open). We recompute from position history.
        for pos in open_positions:
            self.positions[pos.symbol] = pos
        closed = await self.repo.positions_closed_between(
            datetime(1970, 1, 1, tzinfo=UTC), self.clock.now())
        for pos in closed:
            realized += pos.realized_pnl
        invested = sum((p.qty * p.avg_entry_price + p.fees_paid for p in open_positions), D0)
        self.cash = self.starting_cash + realized - invested
        for order in await self.repo.orders_with_status(
                [OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value]):
            self.open_orders[order.id] = order
        log.info("paper engine restored: cash=%s, %d open positions, %d open orders",
                 self.cash, len(self.positions), len(self.open_orders))

    # ── submission ───────────────────────────────────────────────────────────

    async def submit(self, intent: TradeIntent) -> Order:
        """Persist intent BEFORE creating the order (audit + crash safety)."""
        now = self.clock.now()
        await self.repo.store_intent(intent)
        self._intents[intent.id] = intent
        order = Order(
            intent_id=intent.id,
            client_order_id=f"paper-{intent.id[:24]}",
            venue=Venue.PAPER,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            quantity=intent.quantity,
            price=intent.limit_price,
            status=OrderStatus.SUBMITTED,
            strategy=intent.strategy,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=24) if intent.order_type == OrderType.LIMIT else None,
        )
        await self.repo.store_order(order)
        self.open_orders[order.id] = order
        ticker = self._last_ticker.get(intent.symbol)
        if ticker is not None:
            await self._try_fill_order(order, ticker)  # fills only once latency has elapsed
        return order

    async def cancel(self, order_id: str, reason: str = "canceled") -> bool:
        order = self.open_orders.pop(order_id, None)
        if order is None:
            return False
        order.status = OrderStatus.CANCELED
        order.error = reason
        order.updated_at = self.clock.now()
        await self.repo.update_order(order)
        await self._publish(Topics.ORDER_UPDATE, order)
        return True

    # ── market data ──────────────────────────────────────────────────────────

    async def on_book_ticker(self, ticker: BookTicker) -> None:
        self._last_ticker[ticker.symbol] = ticker
        now = self.clock.now()
        for order in list(self.open_orders.values()):
            if order.symbol != ticker.symbol:
                continue
            if order.expires_at is not None and now >= order.expires_at:
                await self.cancel(order.id, "expired")
                continue
            await self._try_fill_order(order, ticker)
        await self._check_position_exits(ticker)

    # ── fills ────────────────────────────────────────────────────────────────

    def _active(self, order: Order) -> bool:
        latency = timedelta(milliseconds=self.costs.latency_ms)
        return self.clock.now() >= order.created_at + latency

    async def _try_fill_order(self, order: Order, ticker: BookTicker) -> None:
        if not self._active(order):
            return
        remaining = order.quantity - order.filled_qty
        if remaining <= 0:
            return
        slip = Decimal(str(self.costs.base_slippage_bps)) / Decimal("10000")

        if order.order_type == OrderType.MARKET:
            if order.side == OrderSide.BUY:
                price = Decimal(str(ticker.ask_price)) * (1 + slip)
                available = Decimal(str(ticker.ask_qty))
            else:
                price = Decimal(str(ticker.bid_price)) * (1 - slip)
                available = Decimal(str(ticker.bid_qty))
            qty = min(remaining, available) if available > 0 else remaining
            await self._fill(order, price, qty, is_maker=False)
        else:  # LIMIT
            assert order.price is not None
            if order.side == OrderSide.BUY and Decimal(str(ticker.ask_price)) <= order.price:
                qty = min(remaining, Decimal(str(ticker.ask_qty)) or remaining)
                await self._fill(order, order.price, qty, is_maker=True)
            elif order.side == OrderSide.SELL and Decimal(str(ticker.bid_price)) >= order.price:
                qty = min(remaining, Decimal(str(ticker.bid_qty)) or remaining)
                await self._fill(order, order.price, qty, is_maker=True)

    async def _fill(self, order: Order, price: Decimal, qty: Decimal, *, is_maker: bool) -> None:
        if qty <= 0:
            return
        now = self.clock.now()
        fee_bps = self.costs.maker_fee_bps if is_maker else self.costs.taker_fee_bps
        fee = price * qty * Decimal(str(fee_bps)) / Decimal("10000")
        fill = Fill(order_id=order.id, venue=Venue.PAPER, symbol=order.symbol, side=order.side,
                    price=price, qty=qty, fee=fee, timestamp=now, is_maker=is_maker)
        await self.repo.store_fill(fill)

        prev_filled = order.filled_qty
        order.filled_qty += qty
        if order.avg_fill_price is None:
            order.avg_fill_price = price
        else:
            order.avg_fill_price = (
                (order.avg_fill_price * prev_filled + price * qty) / order.filled_qty)
        order.status = (OrderStatus.FILLED if order.filled_qty >= order.quantity
                        else OrderStatus.PARTIALLY_FILLED)
        order.updated_at = now
        await self.repo.update_order(order)
        if order.status == OrderStatus.FILLED:
            self.open_orders.pop(order.id, None)
        await self._publish(Topics.ORDER_UPDATE, order)

        if order.side == OrderSide.BUY:
            await self._apply_buy(order, price, qty, fee)
        else:
            await self._apply_sell(order, price, qty, fee)

    async def _apply_buy(self, order: Order, price: Decimal, qty: Decimal, fee: Decimal) -> None:
        self.cash -= price * qty + fee
        intent = self._intents.get(order.intent_id)
        pos = self.positions.get(order.symbol)
        now = self.clock.now()
        if pos is None:
            pos = Position(
                venue=Venue.PAPER, symbol=order.symbol, direction=Direction.LONG,
                qty=qty, avg_entry_price=price,
                stop_price=intent.hypothetical_stop if intent else None,
                target_price=intent.hypothetical_target if intent else None,
                strategy=order.strategy, signal_id=intent.signal_id if intent else "",
                opened_at=now, fees_paid=fee,
            )
            self.positions[order.symbol] = pos
            await self.repo.store_position(pos)
            await self._publish(Topics.POSITION_UPDATE, pos)
        else:
            total_qty = pos.qty + qty
            pos.avg_entry_price = (pos.avg_entry_price * pos.qty + price * qty) / total_qty
            pos.qty = total_qty
            pos.fees_paid += fee
            await self.repo.update_position(pos)
            await self._publish(Topics.POSITION_UPDATE, pos)

    async def _apply_sell(self, order: Order, price: Decimal, qty: Decimal, fee: Decimal) -> None:
        self.cash += price * qty - fee
        pos = self.positions.get(order.symbol)
        if pos is None:
            log.error("sell fill without open position on %s — accounting only", order.symbol)
            return
        pnl = (price - pos.avg_entry_price) * qty - fee
        pos.realized_pnl += pnl
        pos.fees_paid += fee
        pos.qty -= qty
        if pos.qty <= 0:
            pos.closed_at = self.clock.now()
            pos.close_reason = order.error or "exit"
            self.positions.pop(order.symbol, None)
        await self.repo.update_position(pos)
        await self._publish(Topics.POSITION_UPDATE, pos)
        if pos.closed_at is not None and self.on_position_closed is not None:
            await self.on_position_closed(pos, float(pos.realized_pnl))

    # ── exits ────────────────────────────────────────────────────────────────

    async def _check_position_exits(self, ticker: BookTicker) -> None:
        pos = self.positions.get(ticker.symbol)
        if pos is None or not pos.is_open:
            return
        bid = Decimal(str(ticker.bid_price))
        if pos.stop_price is not None and bid <= pos.stop_price:
            await self.close_position(ticker.symbol, reason="stop")
        elif pos.target_price is not None and bid >= pos.target_price:
            await self.close_position(ticker.symbol, reason="target")

    async def close_position(self, symbol: str, *, reason: str) -> Order | None:
        """Market-sell the whole open position (stop/target/structure exit/operator)."""
        pos = self.positions.get(symbol)
        if pos is None or not pos.is_open:
            return None
        # a synthetic exit intent keeps the audit chain complete
        now = self.clock.now()
        intent = TradeIntent(
            signal_id=pos.signal_id, symbol=symbol, direction=Direction.FLAT,
            side=OrderSide.SELL, order_type=OrderType.MARKET,
            reference_price=pos.avg_entry_price, quantity=pos.qty,
            reason=f"exit: {reason}", venue=Venue.PAPER, strategy=pos.strategy, created_at=now,
        )
        await self.repo.store_intent(intent)
        self._intents[intent.id] = intent
        order = Order(
            intent_id=intent.id, client_order_id=f"paper-{intent.id[:24]}", venue=Venue.PAPER,
            symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET,
            quantity=pos.qty, status=OrderStatus.SUBMITTED, strategy=pos.strategy,
            created_at=now, updated_at=now, error=reason,
        )
        await self.repo.store_order(order)
        self.open_orders[order.id] = order
        ticker = self._last_ticker.get(symbol)
        if ticker is not None:
            await self._try_fill_order(order, ticker)
        return order

    # ── accounting ───────────────────────────────────────────────────────────

    def equity(self) -> Decimal:
        total = self.cash
        for pos in self.positions.values():
            ticker = self._last_ticker.get(pos.symbol)
            mark = Decimal(str(ticker.mid)) if ticker else pos.avg_entry_price
            total += pos.qty * mark
        return total

    def drawdown_pct(self) -> float:
        eq = self.equity()
        self._peak_equity = max(self._peak_equity, eq)
        if self._peak_equity <= 0:
            return 0.0
        return float((self._peak_equity - eq) / self._peak_equity * 100)

    def unrealized_pnl(self) -> Decimal:
        total = D0
        for pos in self.positions.values():
            ticker = self._last_ticker.get(pos.symbol)
            if ticker:
                total += pos.unrealized_pnl(Decimal(str(ticker.mid)))
        return total

    async def _publish(self, topic: str, item) -> None:
        if self.bus is not None:
            await self.bus.publish(topic, item)
