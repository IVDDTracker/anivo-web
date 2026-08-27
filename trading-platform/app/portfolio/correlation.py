"""Rolling cross-asset correlation and correlated-exposure estimation.

Crypto assets correlate strongly under stress: four longs in BTC/ETH/SOL/BNB are
close to one levered bet. The risk engine uses `correlated_notional` to cap that.
Fail-safe: if we cannot compute a correlation (insufficient overlapping history)
we ASSUME the pair is correlated (1.0).
"""

from __future__ import annotations

import numpy as np

from app.models.market import Candle


class CorrelationEngine:
    def __init__(self, window: int = 240, min_overlap: int = 60) -> None:
        self.window = window
        self.min_overlap = min_overlap
        self._returns: dict[str, dict[int, float]] = {}  # symbol -> {bar_epoch: log return}

    def update(self, symbol: str, candles: list[Candle]) -> None:
        closes = [(int(c.open_time.timestamp()), c.close) for c in candles[-(self.window + 1):]]
        rets: dict[int, float] = {}
        for (_t0, p0), (t1, p1) in zip(closes, closes[1:], strict=False):
            if p0 > 0 and p1 > 0:
                rets[t1] = float(np.log(p1 / p0))
        self._returns[symbol] = rets

    def correlation(self, a: str, b: str) -> float:
        """Pairwise rolling correlation; 1.0 when unknown (fail-safe assumption)."""
        if a == b:
            return 1.0
        ra, rb = self._returns.get(a), self._returns.get(b)
        if not ra or not rb:
            return 1.0
        common = sorted(set(ra) & set(rb))[-self.window:]
        if len(common) < self.min_overlap:
            return 1.0
        va = np.array([ra[t] for t in common])
        vb = np.array([rb[t] for t in common])
        if va.std(ddof=0) < 1e-12 or vb.std(ddof=0) < 1e-12:
            return 1.0
        return float(np.corrcoef(va, vb)[0, 1])

    def correlated_notional(self, candidate_symbol: str, open_notional: dict[str, float],
                            threshold: float = 0.7) -> float:
        """Total open notional of positions correlated (|rho| >= threshold) with candidate."""
        total = 0.0
        for symbol, notional in open_notional.items():
            if abs(self.correlation(candidate_symbol, symbol)) >= threshold:
                total += abs(notional)
        return total

    def matrix(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        return {a: {b: round(self.correlation(a, b), 3) for b in symbols} for a in symbols}
