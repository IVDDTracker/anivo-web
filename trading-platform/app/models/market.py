"""Market data domain models."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, ConfigDict, Field


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    timeframe: str  # 1m, 5m, 15m, 1h, 4h, 1d
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)
    quote_volume: float = Field(default=0.0, ge=0)
    trades: int = Field(default=0, ge=0)
    taker_buy_base: float = Field(default=0.0, ge=0)
    closed: bool = True


class BookTicker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    bid_price: float = Field(gt=0)
    bid_qty: float = Field(ge=0)
    ask_price: float = Field(gt=0)
    ask_qty: float = Field(ge=0)
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else 0.0


class TradeTick(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    price: float = Field(gt=0)
    qty: float = Field(gt=0)
    timestamp: datetime
    is_buyer_maker: bool  # True → aggressive SELL hit the bid
    trade_id: int


class DepthLevel(BaseModel):
    price: float
    qty: float


class DepthSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    timestamp: datetime
    bids: list[DepthLevel]  # descending price
    asks: list[DepthLevel]  # ascending price

    def imbalance(self, levels: int = 10) -> float:
        """(bidVol - askVol) / (bidVol + askVol) over top N levels; 0 when empty."""
        bid_vol = sum(level.qty for level in self.bids[:levels])
        ask_vol = sum(level.qty for level in self.asks[:levels])
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0


class SymbolRules(BaseModel):
    """Exchange trading rules for a symbol, from exchangeInfo filters.

    Quantization follows the official filter definitions (filters.md):
    price % tickSize == 0, qty % stepSize == 0, price*qty >= minNotional.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    base_asset: str
    quote_asset: str
    status: str = "TRADING"
    tick_size: Decimal = Decimal("0.01")
    min_price: Decimal = Decimal("0")
    max_price: Decimal = Decimal("0")  # 0 → disabled
    step_size: Decimal = Decimal("0.00001")
    min_qty: Decimal = Decimal("0")
    max_qty: Decimal = Decimal("0")  # 0 → disabled
    min_notional: Decimal = Decimal("0")
    apply_min_notional_to_market: bool = True
    base_precision: int = 8
    quote_precision: int = 8

    def quantize_price(self, price: Decimal) -> Decimal:
        if self.tick_size <= 0:
            return price
        return (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def quantize_qty(self, qty: Decimal) -> Decimal:
        if self.step_size <= 0:
            return qty
        return (qty / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def validate_order(self, price: Decimal, qty: Decimal, *, is_market: bool = False) -> list[str]:
        """Return list of violations (empty → valid)."""
        problems: list[str] = []
        if not is_market:
            if self.tick_size > 0 and (price % self.tick_size) != 0:
                problems.append(f"price {price} violates tickSize {self.tick_size}")
            if self.min_price > 0 and price < self.min_price:
                problems.append(f"price {price} < minPrice {self.min_price}")
            if self.max_price > 0 and price > self.max_price:
                problems.append(f"price {price} > maxPrice {self.max_price}")
        if self.step_size > 0 and (qty % self.step_size) != 0:
            problems.append(f"qty {qty} violates stepSize {self.step_size}")
        if self.min_qty > 0 and qty < self.min_qty:
            problems.append(f"qty {qty} < minQty {self.min_qty}")
        if self.max_qty > 0 and qty > self.max_qty:
            problems.append(f"qty {qty} > maxQty {self.max_qty}")
        if self.min_notional > 0 and (not is_market or self.apply_min_notional_to_market):
            if price * qty < self.min_notional:
                problems.append(f"notional {price * qty} < minNotional {self.min_notional}")
        return problems
