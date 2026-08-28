"""Map tweet symbol candidates → tradable USDⓈ-M futures symbols (spec §4).

Only PERPETUAL USDT contracts with status TRADING map; everything else → SKIP.
Also carries per-symbol exchange filters (tickSize/stepSize/minNotional) with
Decimal quantization helpers used by sizing and order placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def quantize_price(self, price: Decimal) -> Decimal:
        if self.tick_size <= 0:
            return price
        return (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def quantize_qty(self, qty: Decimal) -> Decimal:
        if self.step_size <= 0:
            return qty
        return (qty / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def violations(self, price: Decimal, qty: Decimal) -> list[str]:
        problems = []
        if self.step_size > 0 and qty % self.step_size != 0:
            problems.append(f"qty {qty} violates stepSize {self.step_size}")
        if self.min_qty > 0 and qty < self.min_qty:
            problems.append(f"qty {qty} < minQty {self.min_qty}")
        if self.min_notional > 0 and price * qty < self.min_notional:
            problems.append(f"notional {price * qty} < minNotional {self.min_notional}")
        return problems


def parse_symbol_rules(info: dict) -> SymbolRules:
    filters = {f.get("filterType"): f for f in info.get("filters", [])}
    price_f = filters.get("PRICE_FILTER", {})
    lot_f = filters.get("LOT_SIZE", {})
    notional_f = filters.get("MIN_NOTIONAL", {})
    return SymbolRules(
        symbol=str(info["symbol"]),
        base_asset=str(info.get("baseAsset", "")),
        tick_size=Decimal(price_f.get("tickSize", "0")),
        step_size=Decimal(lot_f.get("stepSize", "0")),
        min_qty=Decimal(lot_f.get("minQty", "0")),
        min_notional=Decimal(notional_f.get("notional", "0")),
    )


class SymbolMapper:
    def __init__(self, client, *, refresh_interval_s: float = 3600.0) -> None:
        self.client = client
        self.refresh_interval = timedelta(seconds=refresh_interval_s)
        self._by_base: dict[str, SymbolRules] = {}
        self._loaded_at: datetime | None = None

    async def refresh(self, now: datetime) -> None:
        info = await self.client.exchange_info()
        by_base: dict[str, SymbolRules] = {}
        for entry in info.get("symbols", []):
            if entry.get("status") != "TRADING":
                continue
            if entry.get("contractType") != "PERPETUAL":
                continue
            if entry.get("quoteAsset") != "USDT":
                continue
            rules = parse_symbol_rules(entry)
            by_base[rules.base_asset.upper()] = rules
        self._by_base = by_base
        self._loaded_at = now
        log.info("symbol mapper loaded %d tradable USDT perpetuals", len(by_base))

    async def ensure_fresh(self, now: datetime) -> None:
        if self._loaded_at is None or now - self._loaded_at > self.refresh_interval:
            await self.refresh(now)

    @property
    def known_bases(self) -> set[str]:
        return set(self._by_base)

    def resolve(self, candidates: list[str]) -> SymbolRules | None:
        """First candidate that maps to a live USDT perpetual; None → SKIP."""
        for base in candidates:
            rules = self._by_base.get(base.upper())
            if rules is not None:
                return rules
        return None
