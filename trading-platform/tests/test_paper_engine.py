"""Paper engine tests: fills, fees, latency, partial fills, stops, PnL, restart recovery."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.config.settings import CostConfig
from app.core.bus import EventBus
from app.models.enums import Direction, OrderSide, OrderStatus, OrderType, Venue
from app.models.market import BookTicker
from app.models.orders import TradeIntent
from app.paper.engine import PaperEngine
from app.storage.repositories import OrderRepository

COSTS = CostConfig(taker_fee_bps=10, maker_fee_bps=5, base_slippage_bps=0, latency_ms=200)


@pytest.fixture
async def engine(db, sim_clock):
    repo = OrderRepository(db, Venue.PAPER)
    eng = PaperEngine(costs=COSTS, clock=sim_clock, repo=repo, bus=EventBus(),
                      starting_cash=Decimal("10000"))
    return eng


def ticker(sim_clock, *, bid=100.0, ask=100.1, bid_qty=50.0, ask_qty=50.0,
           symbol="BTCUSDT") -> BookTicker:
    return BookTicker(symbol=symbol, bid_price=bid, bid_qty=bid_qty, ask_price=ask,
                      ask_qty=ask_qty, timestamp=sim_clock.now())


def buy_intent(sim_clock, *, qty="1", order_type=OrderType.MARKET, limit=None,
               stop=None, target=None) -> TradeIntent:
    return TradeIntent(
        signal_id="sig-1", symbol="BTCUSDT", direction=Direction.LONG, side=OrderSide.BUY,
        order_type=order_type, reference_price=Decimal("100"),
        limit_price=Decimal(limit) if limit else None,
        quantity=Decimal(qty),
        hypothetical_stop=Decimal(stop) if stop else None,
        hypothetical_target=Decimal(target) if target else None,
        venue=Venue.PAPER, strategy="test", created_at=sim_clock.now(),
    )


async def tick(engine, sim_clock, *, advance_ms=300, **kw):
    sim_clock.advance_to(sim_clock.now() + timedelta(milliseconds=advance_ms))
    t = ticker(sim_clock, **kw)
    await engine.on_book_ticker(t)
    return t


class TestMarketOrders:
    async def test_market_buy_crosses_spread_with_taker_fee(self, engine, sim_clock):
        await engine.on_book_ticker(ticker(sim_clock))
        order = await engine.submit(buy_intent(sim_clock, qty="1"))
        assert order.status == OrderStatus.SUBMITTED  # latency not yet elapsed
        await tick(engine, sim_clock)
        stored = await engine.repo.get_order(order.id)
        assert stored.status == OrderStatus.FILLED
        assert stored.avg_fill_price == Decimal("100.1")  # bought at the ASK
        expected_fee = Decimal("100.1") * Decimal("0.0010")
        assert engine.cash == Decimal("10000") - Decimal("100.1") - expected_fee
        assert engine.positions["BTCUSDT"].qty == 1

    async def test_latency_prevents_instant_fill(self, engine, sim_clock):
        await engine.on_book_ticker(ticker(sim_clock))
        order = await engine.submit(buy_intent(sim_clock))
        await tick(engine, sim_clock, advance_ms=100)  # < 200ms latency
        assert (await engine.repo.get_order(order.id)).status == OrderStatus.SUBMITTED
        await tick(engine, sim_clock, advance_ms=150)
        assert (await engine.repo.get_order(order.id)).status == OrderStatus.FILLED

    async def test_partial_fill_limited_by_book_qty(self, engine, sim_clock):
        await engine.on_book_ticker(ticker(sim_clock))
        order = await engine.submit(buy_intent(sim_clock, qty="10"))
        await tick(engine, sim_clock, ask_qty=4.0)
        stored = await engine.repo.get_order(order.id)
        assert stored.status == OrderStatus.PARTIALLY_FILLED
        assert stored.filled_qty == Decimal("4")
        await tick(engine, sim_clock, ask_qty=100.0)
        stored = await engine.repo.get_order(order.id)
        assert stored.status == OrderStatus.FILLED and stored.filled_qty == Decimal("10")

    async def test_slippage_applied(self, db, sim_clock):
        repo = OrderRepository(db, Venue.PAPER)
        eng = PaperEngine(costs=CostConfig(taker_fee_bps=0, base_slippage_bps=10, latency_ms=0),
                          clock=sim_clock, repo=repo, starting_cash=Decimal("10000"))
        await eng.on_book_ticker(ticker(sim_clock))
        order = await eng.submit(buy_intent(sim_clock))
        stored = await repo.get_order(order.id)
        assert stored.avg_fill_price == Decimal("100.1") * Decimal("1.001")


class TestLimitOrders:
    async def test_limit_buy_rests_until_touched_then_maker_fee(self, engine, sim_clock):
        await engine.on_book_ticker(ticker(sim_clock))
        order = await engine.submit(
            buy_intent(sim_clock, order_type=OrderType.LIMIT, limit="99.5"))
        await tick(engine, sim_clock)  # ask 100.1 > 99.5 → no fill
        assert (await engine.repo.get_order(order.id)).status == OrderStatus.SUBMITTED
        await tick(engine, sim_clock, bid=99.3, ask=99.4)
        stored = await engine.repo.get_order(order.id)
        assert stored.status == OrderStatus.FILLED
        assert stored.avg_fill_price == Decimal("99.5")
        fills = await engine.repo.fills_between(
            sim_clock.now() - timedelta(days=1), sim_clock.now() + timedelta(days=1))
        assert fills[-1].is_maker

    async def test_limit_order_expires(self, engine, sim_clock):
        await engine.on_book_ticker(ticker(sim_clock))
        order = await engine.submit(
            buy_intent(sim_clock, order_type=OrderType.LIMIT, limit="90"))
        sim_clock.advance_to(sim_clock.now() + timedelta(hours=25))
        await engine.on_book_ticker(ticker(sim_clock))
        assert (await engine.repo.get_order(order.id)).status == OrderStatus.CANCELED


class TestStopsTargetsPnl:
    async def _open(self, engine, sim_clock, stop="95", target="110"):
        await engine.on_book_ticker(ticker(sim_clock))
        await engine.submit(buy_intent(sim_clock, qty="2", stop=stop, target=target))
        await tick(engine, sim_clock)
        assert engine.positions["BTCUSDT"].is_open

    async def test_stop_triggers_market_exit_and_loss(self, engine, sim_clock):
        await self._open(engine, sim_clock)
        closed_events = []
        engine.on_position_closed = lambda pos, pnl: closed_events.append(pnl) or _async_none()
        await tick(engine, sim_clock, bid=94.0, ask=94.1)   # breaches stop → sell order created
        await tick(engine, sim_clock, bid=94.0, ask=94.1)   # latency elapses → fill at bid
        assert "BTCUSDT" not in engine.positions
        closed = await engine.repo.positions_closed_between(
            sim_clock.now() - timedelta(days=1), sim_clock.now() + timedelta(days=1))
        assert len(closed) == 1
        pos = closed[0]
        assert pos.close_reason == "stop"
        assert pos.realized_pnl < 0
        assert closed_events and closed_events[0] < 0

    async def test_target_exit_profits(self, engine, sim_clock):
        await self._open(engine, sim_clock)
        engine.on_position_closed = None
        await tick(engine, sim_clock, bid=111.0, ask=111.1)
        await tick(engine, sim_clock, bid=111.0, ask=111.1)
        closed = await engine.repo.positions_closed_between(
            sim_clock.now() - timedelta(days=1), sim_clock.now() + timedelta(days=1))
        assert closed[0].close_reason == "target"
        assert closed[0].realized_pnl > 0

    async def test_pnl_arithmetic_exact(self, db, sim_clock):
        repo = OrderRepository(db, Venue.PAPER)
        eng = PaperEngine(costs=CostConfig(taker_fee_bps=10, base_slippage_bps=0, latency_ms=0),
                          clock=sim_clock, repo=repo, starting_cash=Decimal("10000"))
        await eng.on_book_ticker(ticker(sim_clock))
        await eng.submit(buy_intent(sim_clock, qty="2"))  # buy 2 @ 100.1, fee 0.2002
        await eng.close_position("BTCUSDT", reason="manual")
        # sell 2 @ bid 100.0, fee 0.2
        closed = (await repo.positions_closed_between(
            sim_clock.now() - timedelta(days=1), sim_clock.now() + timedelta(days=1)))[0]
        expected_pnl = (Decimal("100.0") - Decimal("100.1")) * 2 - Decimal("0.2")
        assert closed.realized_pnl == expected_pnl
        assert eng.cash == (Decimal("10000")
                            - Decimal("100.1") * 2 - Decimal("0.2002")
                            + Decimal("100.0") * 2 - Decimal("0.2"))
        assert eng.equity() == eng.cash  # flat book

    async def test_equity_marks_open_positions(self, engine, sim_clock):
        await self._open(engine, sim_clock)
        await tick(engine, sim_clock, bid=105.0, ask=105.2)
        equity = engine.equity()
        assert equity > Decimal("10000")  # 2 units up ~5


class TestRestartRecovery:
    async def test_restore_rebuilds_positions_and_cash(self, db, sim_clock):
        repo = OrderRepository(db, Venue.PAPER)
        eng1 = PaperEngine(costs=CostConfig(latency_ms=0), clock=sim_clock, repo=repo,
                           starting_cash=Decimal("10000"))
        await eng1.on_book_ticker(ticker(sim_clock))
        await eng1.submit(buy_intent(sim_clock, qty="1", stop="95"))
        assert eng1.positions["BTCUSDT"].is_open

        eng2 = PaperEngine(costs=CostConfig(latency_ms=0), clock=sim_clock, repo=repo,
                           starting_cash=Decimal("10000"))
        await eng2.restore()
        assert "BTCUSDT" in eng2.positions
        assert eng2.positions["BTCUSDT"].stop_price == Decimal("95")
        # cash reflects the open position's cost basis + fees
        assert eng2.cash == eng1.cash

    async def test_duplicate_intent_submission_is_safe(self, engine, sim_clock):
        """Same intent submitted twice (crash between persist and ack) → second rejected."""
        await engine.on_book_ticker(ticker(sim_clock))
        intent = buy_intent(sim_clock)
        await engine.submit(intent)
        with pytest.raises(IntegrityError):
            await engine.submit(intent)  # unique client_order_id + intent PK reject the dup


async def _async_none():
    return None
