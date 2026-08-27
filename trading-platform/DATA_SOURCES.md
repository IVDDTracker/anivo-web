# DATA_SOURCES.md

Every source implements `BaseCollector` (`app/data/collectors/base.py`): async `run()`,
`healthcheck()`, emits `RawEvent`s onto the bus. Raw events are persisted before processing and
carry: `source`, `source_type`, `timestamp_received`, `timestamp_event`, `symbol?`, `raw_payload`,
`normalized_payload`, `confidence`, `source_reliability`, `event_hash`. Events are deduplicated on
`event_hash` (sha256 of source + stable content fields).

## 1. Binance (primary, high trust)

- REST base (public market data): `https://data-api.binance.vision` — klines, exchangeInfo,
  24h ticker, depth snapshots, aggTrades backfill. No API key needed or sent.
- WS base (market data only): `wss://data-stream.binance.vision` — combined streams:
  `<sym>@kline_<tf>`, `<sym>@trade`, `<sym>@aggTrade`, `<sym>@bookTicker`, `<sym>@depth20@100ms`.
- Read-only production account endpoints (optional, `BINANCE_READONLY_*`): `GET /api/v3/account`.
- Testnet: `https://testnet.binance.vision/api` + its WS, with `BINANCE_TESTNET_*` keys.
- Reliability: 1.00 for its own market data.
- Collector behaviors: proactive reconnect < 24 h, ping/pong watchdog, gap detection on kline
  sequence + REST backfill, stale detection (no tick for N s → symbol marked stale), rate-limit
  tracking from response headers, `serverShutdown` handling.

## 2. Telegram

**Control plane** (out of scope as a "source"): commands + notifications, restricted to the
configured admin chat.

**Ingestion**: only chats explicitly configured in `config/telegram_sources.yaml` and reachable
through the Bot API (the operator must add the bot to the chat). Fields per source: `name`,
`identifier` (chat id), `category`, `enabled`, `reliability_score` (0–0.35 cap), `symbols`,
`keywords`. Messages are keyword/entity-scanned for listings, delistings, hacks, exploits,
partnerships, unlocks, outages, regulatory events, rumors, whale mentions, sentiment.
**Telegram is LOW TRUST: capped reliability, never sufficient for a trade alone, and only raises
event confidence when independently confirmed by a higher-trust origin.**

## 3. GitHub

REST v3 (`api.github.com`, version 2022-11-28, ETag conditional polling, default every 5 min)
for repositories configured in `config/github_sources.yaml` (mapped to assets: BTC/ETH/SOL/BNB +
selected protocol repos). Event types: releases, tags, notable commits, security advisories,
archival, activity anomalies (commit-rate z-score), major issues/PRs (by reactions/comments).
Classified into `ExternalEvent` with `asset`, `event_type`, `importance`, `sentiment` (neutral by
default — more commits ≠ bullish), `confidence`, `source_quality` (0.6 default; 0.9 for security
advisories from the project itself).

## 4. News / RSS / external providers

Generic `ExternalDataProvider` interface: `fetch()`, `normalize()`, `healthcheck()`,
`score_reliability()`. Configured in `config/rss_sources.yaml` — official project blogs,
exchange announcement feeds, regulator feeds, quality crypto/macro news. Stdlib XML parsing,
per-source category and reliability. The Alternative.me Fear & Greed index is included as an
optional JSON provider (sentiment feature only).

## Source reliability hierarchy (defaults, configurable)

| Origin | Score |
|---|---|
| Official exchange announcement | 0.95 |
| Official protocol/project source (incl. GitHub security advisory) | 0.90 |
| Regulator / government | 0.90 |
| Established news outlet | 0.70 |
| Multiple independent secondary sources | computed via confirmation |
| Social/aggregator | 0.40 |
| Telegram rumor | ≤ 0.35 |

## Cross-source confirmation & decay

Events are clustered by (assets, category, fuzzy headline hash + time window). Confirmations count
**distinct origin domains/publishers**, not copies — syndicated duplicates of one origin count
once (`novelty` drops instead). Confidence = f(base reliability, confirmation_count, novelty) and
decays exponentially with per-category half-lives (rumor 2 h, news 12 h, confirmed incident 24 h,
regulatory 72 h). A 12-hour-old rumor ≈ noise. All parameters in `app/config/settings.py`.
