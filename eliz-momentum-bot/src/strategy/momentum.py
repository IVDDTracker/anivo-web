"""Real-time momentum tracker (spec §7): the state the reversal score reads.

Fed tick-by-tick (aggTrades / bookTicker / depth5). Identical code path in live
and backtest (the simulator drives the same handlers with historical ticks).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel

from src.core.domain import AggTrade, BookTop


class MomentumMetrics(BaseModel):
    timestamp: datetime
    entry_price: float
    current_price: float
    return_from_entry_pct: float
    peak_price: float
    peak_time: datetime
    drawdown_from_peak_pct: float
    peak_gain_pct: float                 # peak vs entry
    seconds_since_new_high: float
    seconds_since_entry: float
    trade_rate_recent: float             # trades/sec in recent half-window
    trade_rate_earlier: float
    velocity_ratio: float                # recent/earlier (1.0 → unchanged)
    buy_volume: float                    # quote vol, aggressive buys, flow window
    sell_volume: float
    buy_share: float                     # buy/(buy+sell), 0.5 neutral
    depth_imbalance: float               # (bid-ask)/(bid+ask) top5, + = bid heavy
    momentum_short_pct: float            # price change over momentum window
    vwap_since_entry: float
    price_vs_vwap_pct: float
    spread_pct: float


@dataclass
class MomentumTracker:
    entry_price: float
    entry_time: datetime
    flow_window_s: float = 10.0
    momentum_window_s: float = 8.0
    velocity_window_s: float = 20.0

    current_price: float = 0.0
    peak_price: float = 0.0
    peak_time: datetime | None = None
    _trades: deque = field(default_factory=lambda: deque(maxlen=20_000))
    _last_book: BookTop | None = None
    _depth_imbalance: float = 0.0
    _cum_pv: float = 0.0
    _cum_v: float = 0.0

    def __post_init__(self) -> None:
        self.current_price = self.entry_price
        self.peak_price = self.entry_price
        self.peak_time = self.entry_time

    # ── feed handlers ────────────────────────────────────────────────────────

    async def on_trade(self, trade: AggTrade) -> None:
        self._trades.append(trade)
        self.current_price = trade.price
        self._cum_pv += trade.price * trade.qty
        self._cum_v += trade.qty
        if trade.price > self.peak_price:
            self.peak_price = trade.price
            self.peak_time = trade.timestamp

    async def on_book(self, book: BookTop) -> None:
        self._last_book = book

    async def on_depth(self, bids: list, asks: list, ts: datetime) -> None:
        bid_notional = sum(p * q for p, q in bids[:5])
        ask_notional = sum(p * q for p, q in asks[:5])
        total = bid_notional + ask_notional
        self._depth_imbalance = (bid_notional - ask_notional) / total if total > 0 else 0.0

    # ── metrics ──────────────────────────────────────────────────────────────

    def _trades_since(self, cutoff: datetime) -> list[AggTrade]:
        return [t for t in self._trades if t.timestamp >= cutoff]

    def metrics(self, now: datetime) -> MomentumMetrics:
        entry, price, peak = self.entry_price, self.current_price, self.peak_price
        peak_time = self.peak_time or self.entry_time

        flow = self._trades_since(now - timedelta(seconds=self.flow_window_s))
        buy_vol = sum(t.quote_qty for t in flow if not t.is_buyer_maker)
        sell_vol = sum(t.quote_qty for t in flow if t.is_buyer_maker)
        total_flow = buy_vol + sell_vol
        buy_share = buy_vol / total_flow if total_flow > 0 else 0.5

        half = self.velocity_window_s / 2
        recent = self._trades_since(now - timedelta(seconds=half))
        earlier = [t for t in self._trades_since(now - timedelta(seconds=self.velocity_window_s))
                   if t.timestamp < now - timedelta(seconds=half)]
        rate_recent = len(recent) / half
        rate_earlier = len(earlier) / half
        velocity_ratio = rate_recent / rate_earlier if rate_earlier > 0 else \
            (1.0 if rate_recent > 0 else 0.0)

        momentum_cutoff = now - timedelta(seconds=self.momentum_window_s)
        past = [t for t in self._trades if t.timestamp <= momentum_cutoff]
        momentum_ref = past[-1].price if past else entry
        momentum_pct = (price / momentum_ref - 1.0) * 100.0 if momentum_ref > 0 else 0.0

        vwap = self._cum_pv / self._cum_v if self._cum_v > 0 else entry
        return MomentumMetrics(
            timestamp=now,
            entry_price=entry,
            current_price=price,
            return_from_entry_pct=(price / entry - 1.0) * 100.0 if entry > 0 else 0.0,
            peak_price=peak,
            peak_time=peak_time,
            drawdown_from_peak_pct=(peak - price) / peak * 100.0 if peak > 0 else 0.0,
            peak_gain_pct=(peak / entry - 1.0) * 100.0 if entry > 0 else 0.0,
            seconds_since_new_high=max(0.0, (now - peak_time).total_seconds()),
            seconds_since_entry=max(0.0, (now - self.entry_time).total_seconds()),
            trade_rate_recent=rate_recent,
            trade_rate_earlier=rate_earlier,
            velocity_ratio=velocity_ratio,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            buy_share=buy_share,
            depth_imbalance=self._depth_imbalance,
            momentum_short_pct=momentum_pct,
            vwap_since_entry=vwap,
            price_vs_vwap_pct=(price / vwap - 1.0) * 100.0 if vwap > 0 else 0.0,
            spread_pct=self._last_book.spread_pct if self._last_book else 0.0,
        )
