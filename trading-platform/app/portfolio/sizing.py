"""Position sizing research methods. The RISK ENGINE always has the final cap —
these produce proposals, never entitlements. Losses never increase size (no
martingale by construction: every method scales with current equity and risk %).
"""

from __future__ import annotations

from decimal import Decimal

from app.models.market import SymbolRules


def fixed_fractional_qty(*, equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """Risk a fixed % of equity between entry and stop."""
    if entry <= 0 or stop <= 0 or entry <= stop:
        return 0.0
    risk_capital = equity * risk_pct / 100.0
    return risk_capital / (entry - stop)


def atr_qty(*, equity: float, risk_pct: float, entry: float, atr: float,
            atr_mult: float = 2.0) -> float:
    """Volatility-adjusted: stop distance expressed in ATR multiples."""
    if entry <= 0 or atr <= 0:
        return 0.0
    return fixed_fractional_qty(equity=equity, risk_pct=risk_pct, entry=entry,
                                stop=entry - atr_mult * atr)


def apply_caps(qty: float, *, entry: float, equity: float, max_notional_pct: float,
               rules: SymbolRules | None = None) -> float:
    """Notional cap + exchange filters. Returns 0 if the result can't satisfy filters."""
    if qty <= 0 or entry <= 0:
        return 0.0
    max_notional = equity * max_notional_pct / 100.0
    qty = min(qty, max_notional / entry)
    if rules is not None:
        quantized = rules.quantize_qty(Decimal(str(qty)))
        if rules.min_qty > 0 and quantized < rules.min_qty:
            return 0.0
        if rules.min_notional > 0 and Decimal(str(entry)) * quantized < rules.min_notional:
            return 0.0
        return float(quantized)
    return qty
