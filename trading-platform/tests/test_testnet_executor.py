"""Testnet executor: filters, persist-before-send, idempotency, reconciliation."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from app.core.errors import FilterViolation
from app.core.hashing import deterministic_client_order_id
from app.core.state import StateMachine
from app.execution.binance_client import BinanceSignedClient
from app.execution.testnet import TestnetExecutor as TnExecutor
from app.execution.testnet import TestnetReconciler as TnReconciler
from app.models.enums import Direction, OrderSide, OrderStatus, OrderType, Venue
from app.models.market import SymbolRules
from app.models.orders import TradeIntent
from app.storage.repositories import OrderRepository

BASE = "https://testnet.binance.vision"
RULES = {
    "BTCUSDT": SymbolRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        tick_size=Decimal("0.01"), step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"), min_notional=Decimal("5"),
    )
}


@pytest.fixture
def executor(db, sim_clock):
    client = BinanceSignedClient(BASE, "test-key", "test-secret")
    repo = OrderRepository(db, Venue.TESTNET)
    return TnExecutor(client, repo, sim_clock, rules=RULES), repo


def intent(sim_clock, *, qty="0.001234567", order_type=OrderType.MARKET, limit=None):
    return TradeIntent(
        signal_id="sig", symbol="BTCUSDT", direction=Direction.LONG, side=OrderSide.BUY,
        order_type=order_type, reference_price=Decimal("50000"),
        limit_price=Decimal(limit) if limit else None, quantity=Decimal(qty),
        venue=Venue.TESTNET, strategy="test", created_at=sim_clock.now(),
    )


def ok_order_response(status="FILLED", executed="0.00123", quote="61.5"):
    return httpx.Response(200, json={
        "orderId": 42, "clientOrderId": "x", "status": status,
        "executedQty": executed, "cummulativeQuoteQty": quote,
        "fills": [{"commission": "0.05", "commissionAsset": "USDT"}],
    })


class TestSubmission:
    @respx.mock
    async def test_quantity_quantized_and_filled(self, executor, sim_clock):
        exec_, repo = executor
        route = respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response())
        order = await exec_.submit(intent(sim_clock))
        assert order.quantity == Decimal("0.00123")  # floored to stepSize
        assert "quantity=0.00123&" in str(route.calls[0].request.url)
        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price == Decimal("61.5") / Decimal("0.00123")
        positions = await repo.open_positions()
        assert len(positions) == 1 and positions[0].qty == Decimal("0.00123")

    async def test_below_min_notional_rejected_before_send(self, executor, sim_clock):
        exec_, repo = executor
        with pytest.raises(FilterViolation):
            await exec_.submit(intent(sim_clock, qty="0.00002"))  # ~1 USDT < 5
        assert await repo.orders_with_status([s.value for s in OrderStatus]) == []

    @respx.mock
    async def test_limit_price_quantized(self, executor, sim_clock):
        exec_, _ = executor
        route = respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response(status="NEW", executed="0", quote="0"))
        order = await exec_.submit(intent(sim_clock, order_type=OrderType.LIMIT,
                                          limit="49999.999"))
        assert order.price == Decimal("49999.99")
        assert "timeInForce=GTC" in str(route.calls[0].request.url)

    @respx.mock
    async def test_client_order_id_deterministic_from_intent(self, executor, sim_clock):
        exec_, _ = executor
        route = respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response())
        it = intent(sim_clock)
        order = await exec_.submit(it)
        assert order.client_order_id == deterministic_client_order_id(it.id)
        assert f"newClientOrderId={order.client_order_id}" in str(route.calls[0].request.url)


class TestUnknownOutcomes:
    @respx.mock
    async def test_timeout_marks_unknown_and_persists(self, executor, sim_clock):
        exec_, repo = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            side_effect=httpx.ConnectTimeout("timeout"))
        order = await exec_.submit(intent(sim_clock))
        assert order.status == OrderStatus.UNKNOWN
        stored = await repo.get_order_by_client_id(order.client_order_id)
        assert stored is not None and stored.status == OrderStatus.UNKNOWN

    @respx.mock
    async def test_5xx_marks_unknown(self, executor, sim_clock):
        exec_, _ = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=httpx.Response(502, json={}))
        order = await exec_.submit(intent(sim_clock))
        assert order.status == OrderStatus.UNKNOWN

    @respx.mock
    async def test_resubmit_finds_existing_order_no_duplicate(self, executor, sim_clock):
        """API timeout after submission must NOT duplicate the order."""
        exec_, repo = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            side_effect=httpx.ConnectTimeout("timeout"))
        order = await exec_.submit(intent(sim_clock))
        # the exchange actually accepted it; reconcile discovers that
        respx.get(url__regex=rf"{BASE}/api/v3/order\?.*").mock(
            return_value=ok_order_response(status="FILLED"))
        post_route = respx.post(url__regex=rf"{BASE}/api/v3/order.*")
        resolved = await exec_.resubmit_unknown(order)
        assert resolved.status == OrderStatus.FILLED
        assert len(post_route.calls) == 1  # ONLY the original send — no resend

    @respx.mock
    async def test_resubmit_resends_with_same_id_when_absent(self, executor, sim_clock):
        exec_, _ = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            side_effect=[httpx.ConnectTimeout("timeout"), ok_order_response(status="NEW",
                                                                            executed="0", quote="0")])
        order = await exec_.submit(intent(sim_clock))
        respx.get(url__regex=rf"{BASE}/api/v3/order\?.*").mock(
            return_value=httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}))
        original_id = order.client_order_id
        resolved = await exec_.resubmit_unknown(order)
        assert resolved.status == OrderStatus.SUBMITTED
        assert resolved.client_order_id == original_id

    @respx.mock
    async def test_duplicate_client_order_id_treated_as_reconcile(self, executor, sim_clock):
        exec_, _ = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=httpx.Response(400, json={
                "code": -2010, "msg": "Duplicate order sent."}))
        order = await exec_.submit(intent(sim_clock))
        assert order.status == OrderStatus.UNKNOWN  # resolved by reconciler, not resent


class TestReconciler:
    @respx.mock
    async def test_reconcile_resolves_unknown_and_syncs_positions(self, executor, sim_clock):
        exec_, repo = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            side_effect=httpx.ConnectTimeout("timeout"))
        await exec_.submit(intent(sim_clock))
        respx.get(url__regex=rf"{BASE}/api/v3/order\?.*").mock(
            return_value=ok_order_response(status="FILLED"))
        state = StateMachine(clock=sim_clock)
        state.mark_started()
        reconciler = TnReconciler(exec_, repo, sim_clock, state=state)
        stats = await reconciler.reconcile_once()
        assert stats["resolved_unknown"] == 1
        assert (await repo.open_positions())[0].qty == Decimal("0.00123")
        assert state.can_open_new_positions("BTCUSDT")[0]

    @respx.mock
    async def test_vanished_open_order_degrades_state(self, executor, sim_clock):
        exec_, repo = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response(status="NEW", executed="0", quote="0"))
        await exec_.submit(intent(sim_clock, order_type=OrderType.LIMIT, limit="49000"))
        respx.get(url__regex=rf"{BASE}/api/v3/order\?.*").mock(
            return_value=httpx.Response(400, json={"code": -2013, "msg": "unknown"}))
        state = StateMachine(clock=sim_clock)
        state.mark_started()
        reconciler = TnReconciler(exec_, repo, sim_clock, state=state)
        stats = await reconciler.reconcile_once()
        assert stats["vanished"] == 1
        assert not state.can_open_new_positions("BTCUSDT")[0]  # entries frozen

    @respx.mock
    async def test_sell_fill_closes_position_with_pnl(self, executor, sim_clock):
        exec_, repo = executor
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response())
        await exec_.submit(intent(sim_clock))
        sell = TradeIntent(
            signal_id="sig", symbol="BTCUSDT", direction=Direction.FLAT, side=OrderSide.SELL,
            order_type=OrderType.MARKET, reference_price=Decimal("52000"),
            quantity=Decimal("0.00123"), venue=Venue.TESTNET, strategy="test",
            created_at=sim_clock.now())
        respx.post(url__regex=rf"{BASE}/api/v3/order.*").mock(
            return_value=ok_order_response(status="FILLED", executed="0.00123", quote="64.0"))
        await exec_.submit(sell)
        assert await repo.open_positions() == []
        from datetime import timedelta

        closed = await repo.positions_closed_between(
            sim_clock.now() - timedelta(days=1), sim_clock.now() + timedelta(days=1))
        assert len(closed) == 1
        # bought at 61.5/0.00123, sold at 64.0/0.00123, fee 0.05 on the sell fill
        expected = Decimal("64.0") - Decimal("61.5") - Decimal("0.05")
        assert abs(closed[0].realized_pnl - expected) < Decimal("1e-12")  # avg-price division dust
