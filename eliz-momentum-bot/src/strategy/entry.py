"""Market-condition entry filter (spec §5): never blind-long a tweet.

Pure decision function over an EntryInputs snapshot so live, paper and
backtest share the exact same logic.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from src.core.config import Settings
from src.core.domain import (
    Classification,
    MarketSnapshot,
    SignalStage,
    SkipReason,
    TweetEvent,
)


class EntryInputs(BaseModel):
    now: datetime
    reference_price: float           # price as close as possible to tweet timestamp
    mid_price: float
    spread_pct: float
    volume_24h_quote: float | None
    bid_liquidity_usdt: float | None  # top-5 notional
    ask_liquidity_usdt: float | None
    feed_staleness_s: float


class EntryDecision(BaseModel):
    approved: bool
    skip_reason: SkipReason | None = None
    detail: str = ""
    snapshot: MarketSnapshot | None = None


def validate_entry(tweet: TweetEvent, classification: Classification, symbol: str,
                   inputs: EntryInputs, cfg: Settings) -> EntryDecision:
    change_pct = ((inputs.mid_price / inputs.reference_price - 1.0) * 100.0
                  if inputs.reference_price > 0 else 0.0)
    snapshot = MarketSnapshot(
        symbol=symbol, timestamp=inputs.now, reference_price=inputs.reference_price,
        current_price=inputs.mid_price, price_change_since_tweet_pct=round(change_pct, 4),
        spread_pct=round(inputs.spread_pct, 4), volume_24h_quote=inputs.volume_24h_quote,
        bid_liquidity_usdt=inputs.bid_liquidity_usdt,
        ask_liquidity_usdt=inputs.ask_liquidity_usdt)

    def skip(reason: SkipReason, detail: str) -> EntryDecision:
        return EntryDecision(approved=False, skip_reason=reason, detail=detail,
                             snapshot=snapshot)

    age = tweet.age_seconds(inputs.now)
    if age > cfg.max_tweet_age_seconds:
        return skip(SkipReason.TWEET_TOO_OLD,
                    f"tweet age {age:.1f}s > {cfg.max_tweet_age_seconds}s")
    if classification.signal_stage == SignalStage.EARLY and not cfg.trade_early_signals:
        return skip(SkipReason.EARLY_SIGNAL_DISABLED,
                    "EARLY signal (TRADE_EARLY_SIGNALS=false; recorded for research)")
    if classification.confidence < cfg.min_confidence:
        return skip(SkipReason.LOW_CONFIDENCE,
                    f"confidence {classification.confidence:.2f} < {cfg.min_confidence}")
    if inputs.feed_staleness_s > cfg.max_data_staleness_seconds:
        return skip(SkipReason.DATA_STALE,
                    f"feed stale {inputs.feed_staleness_s:.1f}s")
    if inputs.reference_price <= 0 or inputs.mid_price <= 0:
        return skip(SkipReason.DATA_STALE, "no usable price")
    if change_pct > cfg.max_chase_percent:
        return skip(SkipReason.PRICE_ALREADY_PUMPED,
                    f"already +{change_pct:.2f}% since tweet (max chase "
                    f"{cfg.max_chase_percent}%) — no FOMO entry")
    if inputs.spread_pct > cfg.max_spread_percent:
        return skip(SkipReason.SPREAD_TOO_HIGH,
                    f"spread {inputs.spread_pct:.3f}% > {cfg.max_spread_percent}%")
    if inputs.volume_24h_quote is None or inputs.volume_24h_quote < cfg.min_24h_volume:
        return skip(SkipReason.LOW_LIQUIDITY,
                    f"24h volume {inputs.volume_24h_quote} < {cfg.min_24h_volume}")
    if (inputs.bid_liquidity_usdt is None or inputs.ask_liquidity_usdt is None
            or min(inputs.bid_liquidity_usdt, inputs.ask_liquidity_usdt)
            < cfg.min_orderbook_liquidity):
        return skip(SkipReason.LOW_LIQUIDITY,
                    f"orderbook liquidity below {cfg.min_orderbook_liquidity} USDT")
    return EntryDecision(approved=True, snapshot=snapshot,
                         detail=f"move since tweet {change_pct:+.2f}%, "
                                f"spread {inputs.spread_pct:.3f}%")


def validate_watch_entry(tweet: TweetEvent, classification: Classification, symbol: str,
                         inputs: EntryInputs, cfg: Settings, *,
                         max_age_seconds: float | None = None) -> EntryDecision:
    """SHORT_ONLY market validation: same quality gates, but NO chase filter —
    a pump having already happened is exactly what we're waiting to fade, and
    the pump/reversal evidence gates live in the session (MONITORING_PUMP)."""
    max_age = max_age_seconds if max_age_seconds is not None else cfg.max_tweet_age_seconds
    change_pct = ((inputs.mid_price / inputs.reference_price - 1.0) * 100.0
                  if inputs.reference_price > 0 else 0.0)
    snapshot = MarketSnapshot(
        symbol=symbol, timestamp=inputs.now, reference_price=inputs.reference_price,
        current_price=inputs.mid_price, price_change_since_tweet_pct=round(change_pct, 4),
        spread_pct=round(inputs.spread_pct, 4), volume_24h_quote=inputs.volume_24h_quote,
        bid_liquidity_usdt=inputs.bid_liquidity_usdt,
        ask_liquidity_usdt=inputs.ask_liquidity_usdt)

    def skip(reason: SkipReason, detail: str) -> EntryDecision:
        return EntryDecision(approved=False, skip_reason=reason, detail=detail,
                             snapshot=snapshot)

    age = tweet.age_seconds(inputs.now)
    if age > max_age:
        return skip(SkipReason.TWEET_TOO_OLD, f"message age {age:.1f}s > {max_age}s")
    if classification.confidence < cfg.min_confidence:
        return skip(SkipReason.LOW_CONFIDENCE,
                    f"confidence {classification.confidence:.2f} < {cfg.min_confidence}")
    if inputs.feed_staleness_s > cfg.max_data_staleness_seconds:
        return skip(SkipReason.DATA_STALE, f"feed stale {inputs.feed_staleness_s:.1f}s")
    if inputs.reference_price <= 0 or inputs.mid_price <= 0:
        return skip(SkipReason.DATA_STALE, "no usable price")
    if inputs.spread_pct > cfg.max_spread_percent:
        return skip(SkipReason.SPREAD_TOO_HIGH,
                    f"spread {inputs.spread_pct:.3f}% > {cfg.max_spread_percent}%")
    if inputs.volume_24h_quote is None or inputs.volume_24h_quote < cfg.min_24h_volume:
        return skip(SkipReason.LOW_LIQUIDITY,
                    f"24h volume {inputs.volume_24h_quote} < {cfg.min_24h_volume}")
    if (inputs.bid_liquidity_usdt is None or inputs.ask_liquidity_usdt is None
            or min(inputs.bid_liquidity_usdt, inputs.ask_liquidity_usdt)
            < cfg.min_orderbook_liquidity):
        return skip(SkipReason.LOW_LIQUIDITY,
                    f"orderbook liquidity below {cfg.min_orderbook_liquidity} USDT")
    return EntryDecision(approved=True, snapshot=snapshot,
                         detail=f"watching for pump ≥ {cfg.min_pump_percent}% then "
                                f"reversal ≥ {cfg.min_reversal_score} "
                                f"(moved {change_pct:+.2f}% since message)")
