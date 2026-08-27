"""THE most important tests: production execution is hard-disabled, everywhere."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.errors import ProductionExecutionDisabled
from app.execution.binance_client import BinanceSignedClient, is_order_mutating
from app.execution.production import ProductionExecutor


class TestProductionExecutorSealed:
    @pytest.fixture
    def executor(self):
        return ProductionExecutor()

    async def test_submit_raises(self, executor):
        with pytest.raises(ProductionExecutionDisabled):
            await executor.submit(object())

    async def test_place_order_raises(self, executor):
        with pytest.raises(ProductionExecutionDisabled):
            await executor.place_order(symbol="BTCUSDT", side="BUY")

    async def test_cancel_raises(self, executor):
        with pytest.raises(ProductionExecutionDisabled):
            await executor.cancel_order("x")
        with pytest.raises(ProductionExecutionDisabled):
            await executor.cancel_all()
        with pytest.raises(ProductionExecutionDisabled):
            await executor.close_position("BTCUSDT")

    def test_cannot_subclass_to_enable(self):
        with pytest.raises(ProductionExecutionDisabled):
            class Sneaky(ProductionExecutor):  # noqa: F841
                async def submit(self, intent):
                    return "executed"

    async def test_no_enable_flag_exists(self):
        assert ProductionExecutor.ENABLED is False
        executor = ProductionExecutor()
        executor.ENABLED = True  # even a mutated instance attribute changes nothing
        with pytest.raises(ProductionExecutionDisabled):
            await executor.submit(object())


class TestTransportRefusesProductionOrders:
    """Layer 2: the signed HTTP client itself refuses order endpoints on prod hosts."""

    def make(self, base):
        return BinanceSignedClient(base, "key", "secret")

    async def test_order_post_refused_on_production(self):
        client = self.make("https://api.binance.com")
        with pytest.raises(ProductionExecutionDisabled):
            await client.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET",
                                   quantity="0.001", client_order_id="ql-x")

    async def test_order_delete_refused_on_production(self):
        client = self.make("https://api.binance.com")
        with pytest.raises(ProductionExecutionDisabled):
            await client.cancel_order(symbol="BTCUSDT", orig_client_order_id="ql-x")

    async def test_refusal_happens_before_any_network_io(self):
        # no respx mock is registered: a network attempt would raise a respx error,
        # so reaching ProductionExecutionDisabled proves nothing was sent
        client = self.make("https://api1.binance.com")
        with respx.mock:
            with pytest.raises(ProductionExecutionDisabled):
                await client.new_order(symbol="BTCUSDT", side="SELL", order_type="MARKET",
                                       quantity="1", client_order_id="ql-y")

    @respx.mock
    async def test_read_endpoints_allowed_on_production(self):
        respx.get(url__regex=r"https://api\.binance\.com/api/v3/account.*").mock(
            return_value=httpx.Response(200, json={"balances": []}))
        client = self.make("https://api.binance.com")
        account = await client.account()
        assert account == {"balances": []}

    @respx.mock
    async def test_orders_allowed_on_testnet(self):
        respx.post(url__regex=r"https://testnet\.binance\.vision/api/v3/order.*").mock(
            return_value=httpx.Response(200, json={
                "orderId": 1, "status": "NEW", "executedQty": "0",
                "cummulativeQuoteQty": "0"}))
        client = self.make("https://testnet.binance.vision/api")  # official doc form
        assert client.base_url == "https://testnet.binance.vision"  # normalized
        resp = await client.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET",
                                      quantity="0.001", client_order_id="ql-z")
        assert resp["status"] == "NEW"

    def test_mutating_endpoint_matrix(self):
        assert is_order_mutating("POST", "/api/v3/order")
        assert is_order_mutating("DELETE", "/api/v3/order")
        assert is_order_mutating("POST", "/api/v3/sor/order")
        assert is_order_mutating("DELETE", "/api/v3/openOrders")
        assert not is_order_mutating("GET", "/api/v3/order")
        assert not is_order_mutating("GET", "/api/v3/account")


class TestSignature:
    def test_official_docs_hmac_example(self):
        """Signature must match the worked example in official rest-api.md."""
        client = BinanceSignedClient(
            "https://testnet.binance.vision", "key",
            "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
        payload = client._sign({
            "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
            "quantity": 1, "price": 0.1, "recvWindow": 5000, "timestamp": 1499827319559,
        })
        assert payload.endswith(
            "signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")
