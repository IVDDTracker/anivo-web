"""Build the event-study dataset from Telegram channel HISTORY (free data).

For each configured channel: fetch up to --limit messages, classify them with
the same rule engine as live, keep the ones that map to a tradable USDT
perpetual, then download per-event aggTrades exactly like the X pipeline.

    python -m src.backtest.tg_history --limit 2000 --horizon-min 35
    python -m src.backtest.event_study          # then analyze as usual
    python -m src.backtest.simulator            # and replay (works unchanged)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from src.backtest.data_fetcher import fetch_ticks
from src.core.clock import utcnow
from src.core.config import get_settings
from src.core.logger import get_logger, setup_logging
from src.exchange.binance_client import BinanceFuturesClient, ExchangeError
from src.exchange.symbol_mapper import SymbolMapper
from src.telegram_source.listener import fetch_channel_history
from src.twitter.classifier import SignalClassifier
from src.twitter.parser import extract_candidates

log = get_logger(__name__)


async def run(args: argparse.Namespace) -> dict:
    cfg = get_settings()
    setup_logging(cfg.log_level)
    if not (cfg.telegram_api_id and cfg.telegram_api_hash and cfg.telegram_session):
        return {"error": "TELEGRAM_API_ID/HASH/SESSION required — run "
                         "`python -m src.telegram_source.login` first"}
    if not cfg.tg_channels:
        return {"error": "TG_SOURCE_CHANNELS is empty"}

    market = BinanceFuturesClient(cfg.fapi_url)
    mapper = SymbolMapper(market)
    await mapper.refresh(utcnow())
    classifier = SignalClassifier()  # rules only: deterministic & reproducible

    events: list[dict] = []
    total_messages = 0
    for channel in cfg.tg_channels:
        history = await fetch_channel_history(
            api_id=cfg.telegram_api_id, api_hash=cfg.telegram_api_hash,
            session=cfg.telegram_session, channel=channel, limit=args.limit)
        total_messages += len(history)
        for event in history:
            candidates = extract_candidates(event.text, mapper.known_bases)
            c = await classifier.classify(event, candidates)
            if not c.is_trade_signal or not c.symbol:
                continue
            rules = mapper.resolve([c.symbol])
            if rules is None:
                continue
            events.append({
                "tweet_id": event.tweet_id, "symbol": rules.symbol,
                "tweet_ts_ms": int(event.created_at.timestamp() * 1000),
                "stage": c.signal_stage.value, "phrases": c.matched_phrases,
                "text": event.text[:280], "channel": channel,
                "rules": {"tick_size": str(rules.tick_size),
                          "step_size": str(rules.step_size),
                          "min_qty": str(rules.min_qty),
                          "min_notional": str(rules.min_notional)}})
        log.info("channel %s: %d messages → %d cumulative signal events",
                 channel, len(history), len(events))

    ticks_dir = cfg.data_dir / "ticks"
    ticks_dir.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0
    for ev in events:
        path = ticks_dir / f"{ev['tweet_id'].replace(':', '-')}_{ev['symbol']}.json"
        if path.exists():
            continue
        start = ev["tweet_ts_ms"] - 120_000
        end = ev["tweet_ts_ms"] + int(args.horizon_min * 60_000)
        try:
            rows = await fetch_ticks(market, ev["symbol"], start, end)
        except ExchangeError as exc:
            log.warning("tick fetch failed for %s: %s", ev["tweet_id"], str(exc)[:120])
            skipped += 1
            continue
        if rows:
            path.write_text(json.dumps(rows))
            fetched += 1
        else:
            skipped += 1  # older than aggTrades retention → excluded, never faked
    # tick filenames contain ':' → normalize ids so event_study/simulator find them
    for ev in events:
        ev["tick_file"] = f"{ev['tweet_id'].replace(':', '-')}_{ev['symbol']}.json"
        ev["tweet_id"] = ev["tweet_id"].replace(":", "-")

    (cfg.data_dir / "events.json").write_text(json.dumps(events, indent=2))
    return {"channels": cfg.tg_channels, "messages_scanned": total_messages,
            "signal_events": len(events), "tick_files_fetched": fetched,
            "events_without_ticks": skipped,
            "next": "python -m src.backtest.event_study"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2000, help="messages per channel")
    parser.add_argument("--horizon-min", type=float, default=35.0)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
