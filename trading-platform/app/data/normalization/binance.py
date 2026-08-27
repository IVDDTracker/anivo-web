"""Binance payload normalization: raw WS/REST JSON → typed domain models + RawEvent.

Field names follow the official web-socket-streams.md / rest-api.md documentation.
Never assume a payload is complete — every accessor is guarded; malformed payloads
raise ValueError and are counted by the collector, not propagated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.clock import utcnow
from app.core.hashing import event_hash
from app.models.enums import SourceType
from app.models.events import RawEvent
from app.models.market import BookTicker, Candle, DepthLevel, DepthSnapshot, SymbolRules, TradeTick

SOURCE = "binance"


def _ms(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts / 1000.0, tz=UTC)


def parse_kline(data: dict) -> tuple[Candle, RawEvent]:
    k = data["k"]
    candle = Candle(
        symbol=str(k["s"]).upper(),
        timeframe=str(k["i"]),
        open_time=_ms(int(k["t"])),
        close_time=_ms(int(k["T"])),
        open=float(k["o"]), high=float(k["h"]), low=float(k["l"]), close=float(k["c"]),
        volume=float(k["v"]), quote_volume=float(k["q"]),
        trades=int(k["n"]), taker_buy_base=float(k["V"]),
        closed=bool(k["x"]),
    )
    raw = RawEvent(
        source=SOURCE, source_type=SourceType.MARKET,
        timestamp_received=utcnow(), timestamp_event=_ms(int(data["E"])),
        symbol=candle.symbol, kind=f"kline_{candle.timeframe}",
        raw_payload=data,
        event_hash=event_hash(SOURCE, "kline", candle.symbol, candle.timeframe,
                              int(k["t"]), bool(k["x"]), k["c"], k["v"]),
    )
    return candle, raw


def parse_trade(data: dict) -> tuple[TradeTick, RawEvent]:
    tick = TradeTick(
        symbol=str(data["s"]).upper(),
        price=float(data["p"]), qty=float(data["q"]),
        timestamp=_ms(int(data["T"])),
        is_buyer_maker=bool(data["m"]),
        trade_id=int(data["t"]),
    )
    raw = RawEvent(
        source=SOURCE, source_type=SourceType.MARKET,
        timestamp_received=utcnow(), timestamp_event=tick.timestamp,
        symbol=tick.symbol, kind="trade", raw_payload=data,
        event_hash=event_hash(SOURCE, "trade", tick.symbol, tick.trade_id),
    )
    return tick, raw


def parse_book_ticker(data: dict) -> BookTicker:
    # bookTicker payloads carry no event time; stamp receive time.
    return BookTicker(
        symbol=str(data["s"]).upper(),
        bid_price=float(data["b"]), bid_qty=float(data["B"]),
        ask_price=float(data["a"]), ask_qty=float(data["A"]),
        timestamp=utcnow(),
    )


def parse_partial_depth(symbol: str, data: dict) -> DepthSnapshot:
    return DepthSnapshot(
        symbol=symbol.upper(),
        timestamp=utcnow(),
        bids=[DepthLevel(price=float(p), qty=float(q)) for p, q in data.get("bids", [])],
        asks=[DepthLevel(price=float(p), qty=float(q)) for p, q in data.get("asks", [])],
    )


def parse_rest_kline(symbol: str, timeframe: str, row: list) -> Candle:
    """REST /api/v3/klines row (official order: openTime, o,h,l,c, v, closeTime, quoteVol, n, takerBase, takerQuote, ignore)."""
    return Candle(
        symbol=symbol.upper(), timeframe=timeframe,
        open_time=_ms(int(row[0])), close_time=_ms(int(row[6])),
        open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
        volume=float(row[5]), quote_volume=float(row[7]), trades=int(row[8]),
        taker_buy_base=float(row[9]), closed=True,
    )


def parse_symbol_rules(info: dict) -> SymbolRules:
    """exchangeInfo symbol entry → SymbolRules (PRICE_FILTER / LOT_SIZE / NOTIONAL / MIN_NOTIONAL)."""
    filters = {f.get("filterType"): f for f in info.get("filters", [])}
    price_f = filters.get("PRICE_FILTER", {})
    lot_f = filters.get("LOT_SIZE", {})
    notional_f = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    apply_to_market = bool(
        notional_f.get("applyMinToMarket", notional_f.get("applyToMarket", True))
    )
    return SymbolRules(
        symbol=str(info["symbol"]).upper(),
        base_asset=str(info.get("baseAsset", "")),
        quote_asset=str(info.get("quoteAsset", "")),
        status=str(info.get("status", "TRADING")),
        tick_size=Decimal(price_f.get("tickSize", "0")),
        min_price=Decimal(price_f.get("minPrice", "0")),
        max_price=Decimal(price_f.get("maxPrice", "0")),
        step_size=Decimal(lot_f.get("stepSize", "0")),
        min_qty=Decimal(lot_f.get("minQty", "0")),
        max_qty=Decimal(lot_f.get("maxQty", "0")),
        min_notional=Decimal(notional_f.get("minNotional", "0")),
        apply_min_notional_to_market=apply_to_market,
        base_precision=int(info.get("baseAssetPrecision", 8)),
        quote_precision=int(info.get("quoteAssetPrecision", 8)),
    )
