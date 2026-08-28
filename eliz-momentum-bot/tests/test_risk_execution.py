"""Risk sizing, kill switch, adapters, duplicate-order prevention, live guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from src.core.config import Settings
from src.core.domain import BookTop, OrderIntent, OrderSide, OrderStatus
from src.exchange.binance_client import (
    BinanceFuturesClient,
    LiveTradingDisabled,
    OrderOutcomeUnknown,
)
from src.exchange.symbol_mapper import SymbolMapper, SymbolRules
from src.execution.adapters import LiveAdapter, PaperAdapter, client_order_id
from src.execution.order_manager import OrderManager
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskManager
from src.storage.database import Repo

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CFG = Settings(_env_file=None)
RULES = SymbolRules(symbol="TAOUSDT", base_asset="TAO", tick_size=Decimal("0.01"),
                    step_size=Decimal("0.1"), min_qty=Decimal("0.1"),
                    min_notional=Decimal("5"))


def book(bid=99.95, ask=100.05) -> BookTop:
    return BookTop(bid_price=bid, bid_qty=50, ask_price=ask, ask_qty=50, timestamp=T0)


def intent(qty="1.0", side=OrderSide.BUY, iid=None) -> OrderIntent:
    kw = dict(session_id="s1", symbol="TAOUSDT", side=side, quantity=Decimal(qty),
              created_at=T0)
    if iid:
        kw["id"] = iid
    return OrderIntent(**kw)


class TestRiskSizing:
    @pytest.fixture
    async def risk(self, db, sim_clock):
        r = RiskManager(cfg=CFG, repo=Repo(db), kill=KillSwitch())
        await r.restore(sim_clock.now())
        return r

    async def test_risk_based_size(self, risk, sim_clock):
        # wide stop: 10 → 9 (1.0 distance) → qty = 5 USDT risk / 1.0 = 5.0;
        # notional 50 < 100 cap, so the RISK leg binds
        s = risk.size_entry(price=10.0, stop_price=9.0, rules=RULES, now=sim_clock.now())
        assert s.approved and s.quantity == Decimal("5.0")
        assert s.est_max_loss_usdt <= CFG.max_risk_per_trade_usdt
        assert s.notional_usdt <= CFG.max_position_notional_usdt
        # tight stop at higher price: the NOTIONAL cap binds instead
        s2 = risk.size_entry(price=100.0, stop_price=98.5, rules=RULES, now=sim_clock.now())
        assert s2.approved and s2.notional_usdt == pytest.approx(100.0)
        assert s2.est_max_loss_usdt < CFG.max_risk_per_trade_usdt

    async def test_notional_cap_binds_for_tight_stops(self, risk, sim_clock):
        s = risk.size_entry(price=100.0, stop_price=99.9, rules=RULES, now=sim_clock.now())
        assert s.approved
        assert s.notional_usdt <= CFG.max_position_notional_usdt + 1e-9

    async def test_whole_capital_never_at_risk(self, risk, sim_clock):
        s = risk.size_entry(price=100.0, stop_price=98.5, rules=RULES, now=sim_clock.now())
        assert s.est_max_loss_usdt < CFG.account_capital * 0.05

    async def test_daily_loss_kill_switch_latches(self, risk, sim_clock):
        await risk.on_leg_closed(-30.0, 0.5, sim_clock.now())  # > 25 USDT daily limit
        s = risk.size_entry(price=100.0, stop_price=98.5, rules=RULES, now=sim_clock.now())
        assert not s.approved and s.skip_reason.value == "KILL_SWITCH"

    async def test_daily_state_survives_restart(self, db, sim_clock):
        r1 = RiskManager(cfg=CFG, repo=Repo(db), kill=KillSwitch())
        await r1.restore(sim_clock.now())
        await r1.on_leg_closed(-30.0, 0.5, sim_clock.now())
        r2 = RiskManager(cfg=CFG, repo=Repo(db), kill=KillSwitch())
        await r2.restore(sim_clock.now())  # fresh process, same day
        assert r2.kill.active  # burned budget cannot be reset by restarting

    async def test_consecutive_losses(self, risk, sim_clock):
        for _ in range(CFG.max_consecutive_losses):
            await risk.on_leg_closed(-1.0, 0.1, sim_clock.now())
        assert "CONSECUTIVE_LOSSES" in risk.kill.reasons

    async def test_win_resets_streak(self, risk, sim_clock):
        await risk.on_leg_closed(-1.0, 0.1, sim_clock.now())
        await risk.on_leg_closed(+2.0, 0.1, sim_clock.now())
        assert risk.consecutive_losses == 0

    async def test_trades_per_day_limit(self, risk, sim_clock):
        for _ in range(CFG.max_trades_per_day):
            await risk.on_trade_opened(sim_clock.now())
        s = risk.size_entry(price=100.0, stop_price=98.5, rules=RULES, now=sim_clock.now())
        assert not s.approved

    async def test_day_roll_resets_counters_not_streak(self, risk, sim_clock):
        await risk.on_leg_closed(-30.0, 0.5, sim_clock.now())
        sim_clock.advance_to(sim_clock.now() + timedelta(days=1))
        s = risk.size_entry(price=100.0, stop_price=98.5, rules=RULES, now=sim_clock.now())
        assert s.approved  # daily budget resets next day
        assert risk.consecutive_losses == 1  # streak does not


class TestPaperAdapter:
    async def test_buy_crosses_spread_with_fee_and_slippage(self, sim_clock):
        adapter = PaperAdapter(CFG, sim_clock)
        result = await adapter.execute(intent("2.0"), book=book())
        assert result.status == OrderStatus.FILLED
        expected = 100.05 * (1 + CFG.paper_slippage_bps / 10_000)
        assert result.executed_price == pytest.approx(expected)
        assert float(result.fee_usdt) == pytest.approx(
            expected * 2.0 * CFG.taker_fee_rate, rel=1e-6)

    async def test_sell_hits_bid(self, sim_clock):
        result = await PaperAdapter(CFG, sim_clock).execute(
            intent("2.0", side=OrderSide.SELL), book=book())
        assert result.executed_price < 100.0

    async def test_no_book_no_fill(self, sim_clock):
        result = await PaperAdapter(CFG, sim_clock).execute(intent(), book=None)
        assert result.status == OrderStatus.REJECTED


class TestLiveGuards:
    def test_live_adapter_requires_double_flag(self, sim_clock):
        cfg = Settings(_env_file=None, MODE="LIVE", ENABLE_LIVE_TRADING=False)
        with pytest.raises(LiveTradingDisabled):
            LiveAdapter(cfg, sim_clock)
        cfg2 = Settings(_env_file=None, MODE="PAPER", ENABLE_LIVE_TRADING=True)
        with pytest.raises(LiveTradingDisabled):
            LiveAdapter(cfg2, sim_clock)

    async def test_transport_refuses_orders_without_allow_trading(self):
        client = BinanceFuturesClient("https://fapi.binance.com", api_key="k",
                                      api_secret="s", allow_trading=False)
        with respx.mock:  # nothing mocked → refusal must happen before network I/O
            with pytest.raises(LiveTradingDisabled):
                await client.new_order(symbol="TAOUSDT", side="BUY", order_type="MARKET",
                                       quantity="1", client_order_id="eb-x")

    @respx.mock
    async def test_timeout_on_order_is_unknown_not_retry(self):
        respx.post(url__regex=r"https://fapi\.binance\.com/fapi/v1/order.*").mock(
            side_effect=httpx.ConnectTimeout("boom"))
        client = BinanceFuturesClient("https://fapi.binance.com", api_key="k",
                                      api_secret="s", allow_trading=True)
        with pytest.raises(OrderOutcomeUnknown):
            await client.new_order(symbol="TAOUSDT", side="BUY", order_type="MARKET",
                                   quantity="1", client_order_id="eb-x")


class TestOrderManagerDedup:
    async def test_same_intent_never_sent_twice(self, db, sim_clock):
        om = OrderManager(PaperAdapter(CFG, sim_clock), Repo(db), sim_clock)
        it = intent("1.0", iid="fixed-intent-id")
        r1 = await om.submit(it, book=book())
        assert r1.status == OrderStatus.FILLED
        r2 = await om.submit(it, book=book())  # crash-retry with the same intent
        assert "duplicate" in r2.error
        rows = await Repo(db).orders_with_status([s.value for s in OrderStatus])
        assert len(rows) == 1

    def test_client_order_id_deterministic(self):
        assert client_order_id("abc") == client_order_id("abc")
        assert client_order_id("abc") != client_order_id("abd")


EXCHANGE_INFO = {"symbols": [
    {"symbol": "TAOUSDT", "status": "TRADING", "contractType": "PERPETUAL",
     "baseAsset": "TAO", "quoteAsset": "USDT",
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
    {"symbol": "OLDUSDT", "status": "SETTLING", "contractType": "PERPETUAL",
     "baseAsset": "OLD", "quoteAsset": "USDT", "filters": []},
    {"symbol": "BTCUSDT_260626", "status": "TRADING", "contractType": "CURRENT_QUARTER",
     "baseAsset": "BTC", "quoteAsset": "USDT", "filters": []},
]}


class TestSymbolMapper:
    @respx.mock
    async def test_only_trading_perpetuals_map(self):
        respx.get(url__regex=r".*/fapi/v1/exchangeInfo").mock(
            return_value=httpx.Response(200, json=EXCHANGE_INFO))
        client = BinanceFuturesClient("https://fapi.binance.com")
        mapper = SymbolMapper(client)
        await mapper.refresh(T0)
        assert mapper.resolve(["TAO"]).symbol == "TAOUSDT"
        assert mapper.resolve(["OLD"]) is None        # not TRADING
        assert mapper.resolve(["RANDOMWORD"]) is None  # spec §4: SKIP
        assert mapper.resolve(["XYZ", "TAO"]).symbol == "TAOUSDT"  # first tradable wins
        assert "TAO" in mapper.known_bases

    def test_quantization(self):
        assert RULES.quantize_qty(Decimal("3.37")) == Decimal("3.3")
        assert RULES.quantize_price(Decimal("100.056")) == Decimal("100.05")
        assert RULES.violations(Decimal("100"), Decimal("0.01"))  # below minQty
