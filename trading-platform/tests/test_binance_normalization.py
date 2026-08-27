"""Payload contract tests against the official documented shapes."""

from __future__ import annotations

from decimal import Decimal

# Shapes copied from official binance-spot-api-docs (web-socket-streams.md / rest-api.md)
KLINE_EVENT = {
    "e": "kline", "E": 1672515782136, "s": "BTCUSDT",
    "k": {
        "t": 1672515780000, "T": 1672515839999, "s": "BTCUSDT", "i": "1m", "f": 100, "L": 200,
        "o": "16500.10", "c": "16510.50", "h": "16511.00", "l": "16499.00", "v": "12.5",
        "n": 100, "x": True, "q": "206380.5", "V": "6.2", "Q": "102400.0", "B": "0",
    },
}
TRADE_EVENT = {
    "e": "trade", "E": 1672515782136, "s": "BTCUSDT", "t": 12345,
    "p": "16510.50", "q": "0.5", "T": 1672515782134, "m": True, "M": True,
}
BOOK_TICKER_EVENT = {
    "u": 400900217, "s": "BTCUSDT", "b": "16510.00", "B": "3.1", "a": "16510.10", "A": "2.4",
}
EXCHANGE_INFO_SYMBOL = {
    "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
    "baseAssetPrecision": 8, "quoteAssetPrecision": 8,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00",
         "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.0",
         "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5.00", "applyMinToMarket": True,
         "maxNotional": "9000000.00", "applyMaxToMarket": False},
    ],
}
REST_KLINE_ROW = [
    1672515780000, "16500.10", "16511.00", "16499.00", "16510.50", "12.5",
    1672515839999, "206380.5", 100, "6.2", "102400.0", "0",
]

from app.data.normalization import binance as norm  # noqa: E402


def test_parse_kline():
    candle, raw = norm.parse_kline(KLINE_EVENT)
    assert candle.symbol == "BTCUSDT" and candle.timeframe == "1m" and candle.closed
    assert candle.open == 16500.10 and candle.close == 16510.50
    assert candle.taker_buy_base == 6.2
    assert raw.kind == "kline_1m" and raw.event_hash


def test_parse_kline_hash_dedups_identical_events():
    _, raw1 = norm.parse_kline(KLINE_EVENT)
    _, raw2 = norm.parse_kline(KLINE_EVENT)
    assert raw1.event_hash == raw2.event_hash


def test_parse_trade():
    tick, raw = norm.parse_trade(TRADE_EVENT)
    assert tick.price == 16510.50 and tick.is_buyer_maker and tick.trade_id == 12345
    assert raw.event_hash == norm.parse_trade(TRADE_EVENT)[1].event_hash


def test_parse_book_ticker():
    bt = norm.parse_book_ticker(BOOK_TICKER_EVENT)
    assert bt.bid_price == 16510.00 and bt.ask_price == 16510.10
    assert 0 < bt.spread_pct < 0.001


def test_parse_symbol_rules_reads_all_filters():
    rules = norm.parse_symbol_rules(EXCHANGE_INFO_SYMBOL)
    assert rules.tick_size == Decimal("0.01")
    assert rules.step_size == Decimal("0.00001")
    assert rules.min_notional == Decimal("5.00")
    assert rules.apply_min_notional_to_market is True


def test_parse_rest_kline_row():
    candle = norm.parse_rest_kline("BTCUSDT", "1m", REST_KLINE_ROW)
    assert candle.open == 16500.10 and candle.trades == 100 and candle.closed


def test_malformed_kline_raises_for_collector_counting():
    import pytest

    with pytest.raises((KeyError, ValueError, TypeError)):
        norm.parse_kline({"e": "kline", "k": {"s": "BTCUSDT"}})
