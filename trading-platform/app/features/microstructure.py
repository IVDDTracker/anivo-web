"""Market microstructure features from live trades / book tickers / depth snapshots.

Rolling in-memory windows per symbol. Each feature exists because it feeds the
fusion layer's microstructure/liquidity scores or the risk engine's spread gate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models.market import BookTicker, DepthSnapshot, TradeTick


@dataclass
class _SymbolMicro:
    trades: deque = field(default_factory=lambda: deque(maxlen=5000))
    tickers: deque = field(default_factory=lambda: deque(maxlen=2000))
    last_depth: DepthSnapshot | None = None


class MicrostructureTracker:
    def __init__(self, window_s: float = 300.0) -> None:
        self.window = timedelta(seconds=window_s)
        self._by_symbol: dict[str, _SymbolMicro] = {}

    def _sm(self, symbol: str) -> _SymbolMicro:
        return self._by_symbol.setdefault(symbol, _SymbolMicro())

    def on_trade(self, tick: TradeTick) -> None:
        self._sm(tick.symbol).trades.append(tick)

    def on_book_ticker(self, ticker: BookTicker) -> None:
        self._sm(ticker.symbol).tickers.append(ticker)

    def on_depth(self, snap: DepthSnapshot) -> None:
        self._sm(snap.symbol).last_depth = snap

    def features(self, symbol: str, now: datetime) -> dict[str, float]:
        sm = self._by_symbol.get(symbol)
        if sm is None:
            return {}
        cutoff = now - self.window
        out: dict[str, float] = {}

        trades = [t for t in sm.trades if t.timestamp >= cutoff]
        if trades:
            # aggressive flow: buyer-maker=True means an aggressive SELL hit the bid
            buy_vol = sum(t.qty * t.price for t in trades if not t.is_buyer_maker)
            sell_vol = sum(t.qty * t.price for t in trades if t.is_buyer_maker)
            total = buy_vol + sell_vol
            out["aggr_buy_quote_vol"] = buy_vol
            out["aggr_sell_quote_vol"] = sell_vol
            out["volume_delta"] = buy_vol - sell_vol
            out["trade_imbalance"] = (buy_vol - sell_vol) / total if total > 0 else 0.0
            out["trade_count"] = float(len(trades))
            first, last = trades[0], trades[-1]
            dt_s = max((last.timestamp - first.timestamp).total_seconds(), 1e-9)
            out["price_velocity_pct_per_min"] = (
                (last.price / first.price - 1.0) * 100.0 * 60.0 / dt_s if dt_s > 1 else 0.0
            )
            half = len(trades) // 2
            if half >= 5:
                vol_recent = sum(t.qty for t in trades[half:])
                vol_earlier = sum(t.qty for t in trades[:half])
                out["volume_acceleration"] = (
                    vol_recent / vol_earlier - 1.0 if vol_earlier > 0 else 0.0
                )

        tickers = [t for t in sm.tickers if t.timestamp >= cutoff]
        if tickers:
            spreads = [t.spread_pct for t in tickers]
            out["spread_pct_mean"] = sum(spreads) / len(spreads)
            out["spread_pct_last"] = tickers[-1].spread_pct
            out["best_bid_qty"] = tickers[-1].bid_qty
            out["best_ask_qty"] = tickers[-1].ask_qty
            top_notional = (
                tickers[-1].bid_qty * tickers[-1].bid_price
                + tickers[-1].ask_qty * tickers[-1].ask_price
            )
            out["top_of_book_notional"] = top_notional
            mids = [t.mid for t in tickers]
            if len(mids) >= 20:
                import numpy as np

                rets = np.diff(np.log(np.asarray(mids)))
                out["tick_realized_vol"] = float(np.std(rets, ddof=0))

        if sm.last_depth is not None and sm.last_depth.timestamp >= cutoff:
            out["depth_imbalance"] = sm.last_depth.imbalance(10)
            bid_notional = sum(level.price * level.qty for level in sm.last_depth.bids[:10])
            ask_notional = sum(level.price * level.qty for level in sm.last_depth.asks[:10])
            out["depth_bid_notional_10"] = bid_notional
            out["depth_ask_notional_10"] = ask_notional
            # crude price-impact estimate: bps to consume top-10 ask liquidity with 10k USDT
            if ask_notional > 0:
                out["impact_est_bps_10k"] = min(10_000.0 / ask_notional, 1.0) * 100.0

        return out
