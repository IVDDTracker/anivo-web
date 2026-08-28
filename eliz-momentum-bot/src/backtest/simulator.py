"""Backtest simulator: replays historical event ticks through the SAME
TradeSession / reversal / risk code the live bot runs (spec §13/§14).

Causality: the session starts at tweet_ts + --latency-s (simulated pipeline
latency) and only ever sees ticks in time order via SimClock — the strategy can
not read the future. The reference price IS allowed to be the first trade at or
after the tweet timestamp (that is exactly what the live bot queries via REST).

Honest limitation (printed in the report): historical order books are not
available from aggTrades, so book-dependent inputs (spread, top-5 liquidity)
are synthesized from a configurable spread assumption; the entry chase filter
uses real trade prices.

    python -m src.backtest.simulator [--latency-s 3] [--spread-bps 4] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.event_study import EventTicks
from src.core.clock import SimClock
from src.core.config import Settings, get_settings
from src.core.domain import (
    AggTrade,
    BookTop,
    Classification,
    Direction,
    SignalAction,
    SignalStage,
    TweetEvent,
    TweetKind,
)
from src.core.logger import get_logger, setup_logging
from src.exchange.symbol_mapper import SymbolRules
from src.execution.adapters import PaperAdapter
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.metrics import build_report
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskManager
from src.storage.database import Database, Repo
from src.strategy.entry import EntryInputs
from src.strategy.session import TradeSession

log = get_logger(__name__)


def _ms_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _book(price: float, spread_bps: float, ts: datetime) -> BookTop:
    half = price * spread_bps / 20_000.0
    return BookTop(bid_price=price - half, bid_qty=50.0, ask_price=price + half,
                   ask_qty=50.0, timestamp=ts)


async def simulate_event(ev: dict, ticks: EventTicks, cfg: Settings, repo: Repo,
                         *, latency_s: float, spread_bps: float) -> dict:
    tweet_ts = _ms_dt(ticks.tweet_ts_ms)
    clock = SimClock(tweet_ts + timedelta(seconds=latency_s))
    rules_raw = ev.get("rules", {})
    rules = SymbolRules(symbol=ev["symbol"], base_asset=ev["symbol"].removesuffix("USDT"),
                        tick_size=Decimal(rules_raw.get("tick_size", "0.0001")),
                        step_size=Decimal(rules_raw.get("step_size", "0.1")),
                        min_qty=Decimal(rules_raw.get("min_qty", "0.1")),
                        min_notional=Decimal(rules_raw.get("min_notional", "5")))
    tweet = TweetEvent(tweet_id=ev["tweet_id"], author_id="", text=ev.get("text", ""),
                       kind=TweetKind.ORIGINAL, created_at=tweet_ts,
                       received_at=clock.now())
    classification = Classification(
        is_trade_signal=True, symbol=rules.base_asset, direction=Direction.LONG,
        confidence=0.8, signal_stage=SignalStage(ev.get("stage", "CONFIRMED")),
        action=SignalAction.BUY, tweet_id=ev["tweet_id"], reason="backtest replay")
    kill = KillSwitch()
    risk = RiskManager(cfg=cfg, repo=repo, kill=kill)
    await risk.restore(clock.now())
    session = TradeSession(
        session_id=str(uuid.uuid4()), tweet=tweet, classification=classification,
        rules=rules, cfg=cfg, clock=clock,
        orders=OrderManager(PaperAdapter(cfg, clock), repo, clock),
        positions=PositionManager(repo, clock, kill), risk=risk, repo=repo)

    # ticks known at decision time: reference = first trade ≥ tweet; mid = last
    # trade ≤ now (never a future trade)
    idx_ref = int((ticks.ts_ms >= ticks.tweet_ts_ms).argmax())
    reference = float(ticks.price[idx_ref])
    now_ms = ticks.tweet_ts_ms + int(latency_s * 1000)
    known = ticks.ts_ms <= now_ms
    if not known.any():
        return {"tweet_id": ev["tweet_id"], "result": "no ticks before decision time"}
    mid = float(ticks.price[known][-1])
    await session.on_book(_book(mid, spread_bps, clock.now()))
    started = await session.start(EntryInputs(
        now=clock.now(), reference_price=reference, mid_price=mid,
        spread_pct=spread_bps / 100.0,
        volume_24h_quote=cfg.min_24h_volume,       # book/volume not in historical data:
        bid_liquidity_usdt=cfg.min_orderbook_liquidity,   # neutral pass-through values
        ask_liquidity_usdt=cfg.min_orderbook_liquidity,
        feed_staleness_s=0.0))
    if not started:
        return {"tweet_id": ev["tweet_id"], "result": f"skipped: {session.sm.history[-1][1]}"}

    future = ticks.ts_ms > now_ms
    for ts_ms, price, qty, is_sell in zip(ticks.ts_ms[future], ticks.price[future],
                                          ticks.qty[future], ticks.is_sell[future], strict=True):
        when = _ms_dt(int(ts_ms))
        if when > clock.now():
            clock.advance_to(when)
        await session.on_book(_book(float(price), spread_bps, when))
        await session.on_trade(AggTrade(price=float(price), qty=float(qty),
                                        timestamp=when, is_buyer_maker=bool(is_sell)))
        if session.done:
            break
    if not session.done:
        # data ran out with a position open → close at last known price
        await session.evaluate(clock.now() + timedelta(seconds=3600))
    return {"tweet_id": ev["tweet_id"], "final_state": session.sm.state.value}


async def run(args: argparse.Namespace) -> dict:
    cfg = get_settings()
    setup_logging(cfg.log_level)
    events_path = cfg.data_dir / "events.json"
    if not events_path.exists():
        return {"error": "data/events.json missing — run src.backtest.data_fetcher first"}
    events = json.loads(events_path.read_text())
    db_path = cfg.data_dir / "backtest.db"
    if db_path.exists():
        db_path.unlink()
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.create_all()
    repo = Repo(db)
    sessions = []
    try:
        for ev in events[: args.limit or len(events)]:
            tick_file = cfg.data_dir / "ticks" / f"{ev['tweet_id']}_{ev['symbol']}.json"
            if not tick_file.exists():
                sessions.append({"tweet_id": ev["tweet_id"], "result": "no tick data"})
                continue
            ticks = EventTicks.from_rows(int(ev["tweet_ts_ms"]),
                                         json.loads(tick_file.read_text()))
            sessions.append(await simulate_event(ev, ticks, cfg, repo,
                                                 latency_s=args.latency_s,
                                                 spread_bps=args.spread_bps))
        report = await build_report(repo)
        return {"assumptions": {
                    "simulated_latency_s": args.latency_s,
                    "synthetic_spread_bps": args.spread_bps,
                    "note": "historical order books unavailable; spread/liquidity "
                            "synthesized — costs are approximations"},
                "sessions": sessions, "performance": report,
                "db": str(db_path)}
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latency-s", type=float, default=3.0,
                        help="simulated tweet→order pipeline latency")
    parser.add_argument("--spread-bps", type=float, default=4.0)
    parser.add_argument("--limit", type=int, default=0, help="max events (0 = all)")
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2, default=str))


if __name__ == "__main__":
    main()
