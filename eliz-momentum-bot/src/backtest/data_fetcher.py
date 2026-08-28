"""Fetch historical data for the event study.

1. Historical tweets of the target account (X API v2 user timeline, paginated —
   the API exposes roughly the most recent 3200 tweets).
2. For every rule-classified signal tweet whose symbol trades on USDⓈ-M
   futures: aggTrades from 2 min before to `--horizon-min` after the tweet
   (millisecond resolution), saved per event to data/ticks/.

Outputs: data/events.json + data/ticks/<tweet_id>_<symbol>.json

    python -m src.backtest.data_fetcher [--max-tweets 3200] [--horizon-min 35]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from src.core.clock import utcnow
from src.core.config import get_settings
from src.core.domain import TweetEvent, TweetKind
from src.core.logger import get_logger, setup_logging
from src.exchange.binance_client import BinanceFuturesClient, ExchangeError
from src.exchange.symbol_mapper import SymbolMapper
from src.twitter.classifier import SignalClassifier
from src.twitter.listener import TWEET_FIELDS, XClient, tweet_kind
from src.twitter.parser import extract_candidates

log = get_logger(__name__)


async def fetch_all_tweets(client: XClient, username: str, max_tweets: int) -> list[dict]:
    user_id = await client.get_user_id(username)
    out: list[dict] = []
    token: str | None = None
    while len(out) < max_tweets:
        params = {"max_results": 100, "exclude": "retweets,replies",
                  "tweet.fields": TWEET_FIELDS}
        if token:
            params["pagination_token"] = token
        data = await client._get(f"/2/users/{user_id}/tweets", params)
        page = data.get("data") or []
        out.extend(page)
        token = (data.get("meta") or {}).get("next_token")
        log.info("fetched %d tweets so far", len(out))
        if not token or not page:
            break
    return out[:max_tweets]


async def fetch_ticks(market: BinanceFuturesClient, symbol: str, start_ms: int,
                      end_ms: int) -> list[list]:
    """Paginate aggTrades over [start_ms, end_ms] via fromId walking."""
    rows: list[list] = []
    batch = await market.agg_trades(symbol, start_ms=start_ms, end_ms=end_ms, limit=1000)
    while batch:
        for t in batch:
            ts = int(t["T"])
            if ts > end_ms:
                return rows
            rows.append([ts, float(t["p"]), float(t["q"]), bool(t["m"])])
        last_id = int(batch[-1]["a"])
        batch = await market.agg_trades(symbol, from_id=last_id + 1, limit=1000)
        await asyncio.sleep(0.15)  # stay well under fapi rate limits
    return rows


async def run(args: argparse.Namespace) -> dict:
    cfg = get_settings()
    setup_logging(cfg.log_level)
    if not cfg.x_bearer_token:
        return {"error": "X_BEARER_TOKEN required to fetch historical tweets"}
    x = XClient(cfg.x_bearer_token, api_base=cfg.x_api_base)
    market = BinanceFuturesClient(cfg.fapi_url)
    mapper = SymbolMapper(market)
    await mapper.refresh(utcnow())
    classifier = SignalClassifier()  # rules only — deterministic, reproducible

    raw_tweets = await fetch_all_tweets(x, cfg.x_target_username, args.max_tweets)
    events: list[dict] = []
    for data in raw_tweets:
        if tweet_kind(data) != TweetKind.ORIGINAL:
            continue
        created = datetime.fromisoformat(
            data["created_at"].replace("Z", "+00:00")).astimezone(UTC)
        tweet = TweetEvent(tweet_id=str(data["id"]), author_id=str(data.get("author_id", "")),
                           text=data.get("text", ""), created_at=created,
                           received_at=created, raw=data)
        candidates = extract_candidates(tweet.text, mapper.known_bases)
        c = await classifier.classify(tweet, candidates)
        if not c.is_trade_signal or not c.symbol:
            continue
        rules = mapper.resolve([c.symbol])
        if rules is None:
            continue
        events.append({"tweet_id": tweet.tweet_id, "symbol": rules.symbol,
                       "tweet_ts_ms": int(created.timestamp() * 1000),
                       "stage": c.signal_stage.value, "phrases": c.matched_phrases,
                       "text": tweet.text[:280],
                       "rules": {"tick_size": str(rules.tick_size),
                                 "step_size": str(rules.step_size),
                                 "min_qty": str(rules.min_qty),
                                 "min_notional": str(rules.min_notional)}})
    log.info("classified %d signal events out of %d tweets", len(events), len(raw_tweets))

    ticks_dir = cfg.data_dir / "ticks"
    ticks_dir.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0
    for ev in events:
        path = ticks_dir / f"{ev['tweet_id']}_{ev['symbol']}.json"
        if path.exists():
            continue
        start = ev["tweet_ts_ms"] - 120_000
        end = ev["tweet_ts_ms"] + int(args.horizon_min * 60_000)
        try:
            rows = await fetch_ticks(market, ev["symbol"], start, end)
        except ExchangeError as exc:
            log.warning("tick fetch failed for %s (%s) — event will be excluded "
                        "from timing stats", ev["tweet_id"], str(exc)[:120])
            skipped += 1
            continue
        if rows:
            path.write_text(json.dumps(rows))
            fetched += 1
        else:
            skipped += 1  # too old for aggTrades retention → excluded, not faked

    (cfg.data_dir / "events.json").write_text(json.dumps(events, indent=2))
    return {"tweets_fetched": len(raw_tweets), "signal_events": len(events),
            "tick_files_fetched": fetched, "events_without_ticks": skipped,
            "next": "python -m src.backtest.event_study"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-tweets", type=int, default=3200)
    parser.add_argument("--horizon-min", type=float, default=35.0)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
