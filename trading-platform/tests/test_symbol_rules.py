"""Exchange filter compliance: tickSize/stepSize/minNotional per official filters.md."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.market import SymbolRules

BTC_RULES = SymbolRules(
    symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
    tick_size=Decimal("0.01"), step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"), min_notional=Decimal("5"),
)


class TestQuantization:
    def test_price_floors_to_tick(self):
        assert BTC_RULES.quantize_price(Decimal("50000.019")) == Decimal("50000.01")

    def test_qty_floors_to_step(self):
        assert BTC_RULES.quantize_qty(Decimal("0.123456789")) == Decimal("0.12345")

    def test_exact_values_unchanged(self):
        assert BTC_RULES.quantize_price(Decimal("50000.01")) == Decimal("50000.01")
        assert BTC_RULES.quantize_qty(Decimal("0.12345")) == Decimal("0.12345")

    def test_validate_passes_clean_order(self):
        assert BTC_RULES.validate_order(Decimal("50000.01"), Decimal("0.001")) == []

    def test_validate_rejects_bad_tick(self):
        problems = BTC_RULES.validate_order(Decimal("50000.011"), Decimal("0.001"))
        assert any("tickSize" in p for p in problems)

    def test_validate_rejects_bad_step(self):
        problems = BTC_RULES.validate_order(Decimal("50000.01"), Decimal("0.000011"))
        assert any("stepSize" in p for p in problems)

    def test_validate_rejects_below_min_notional(self):
        problems = BTC_RULES.validate_order(Decimal("100.00"), Decimal("0.001"))
        assert any("minNotional" in p for p in problems)

    def test_min_notional_market_order_flag(self):
        rules = BTC_RULES.model_copy(update={"apply_min_notional_to_market": False})
        assert rules.validate_order(Decimal("100.00"), Decimal("0.001"), is_market=True) == []


class TestQuantizationProperties:
    @settings(max_examples=200)
    @given(price=st.decimals(min_value="0.00000001", max_value="1000000",
                             allow_nan=False, allow_infinity=False, places=8))
    def test_quantized_price_always_valid(self, price: Decimal):
        q = BTC_RULES.quantize_price(price)
        assert q % BTC_RULES.tick_size == 0
        assert q <= price  # floor, never round up

    @settings(max_examples=200)
    @given(qty=st.decimals(min_value="0.00000001", max_value="100000",
                           allow_nan=False, allow_infinity=False, places=8))
    def test_quantized_qty_always_valid(self, qty: Decimal):
        q = BTC_RULES.quantize_qty(qty)
        assert q % BTC_RULES.step_size == 0
        assert q <= qty
