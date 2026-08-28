# eliz-momentum-bot

Event-driven research & trading system for the hypothesis:

> **ELIZ TWEET → audience reaction → short-term buying pressure → local peak →
> mean reversion.**

It watches `@eliz883` on X in real time, classifies tweets (EARLY vs CONFIRMED
signal), validates market conditions on Binance USDⓈ-M Futures, opens a LONG
into the momentum, tracks the pump tick-by-tick, closes on a behavior-based
**reversal score** (never a fixed timer), and only after a separate
confirmation opens a managed SHORT. Everything — tweets, signals, skips,
orders, PnL, latencies, state transitions — is persisted; Telegram reports
every step.

**This bot does not assume Eliz is a good trader — it measures what her
audience does to price, first, and only then trades it** (spec §23: measure
before trading; uncertainty ⇒ NO TRADE).

---

## ⚠️ Read this before going live

- Default mode is **PAPER** (live market data, simulated fills). LIVE needs
  **two independent flags**: `MODE=LIVE` **and** `ENABLE_LIVE_TRADING=true`.
  With either missing, the live adapter refuses to construct AND the HTTP
  transport refuses order endpoints — belt and suspenders.
- **Run the research first.** The whole point of the event study is to check
  whether the edge exists *after fees, slippage and your real latency*. The
  recommended path: `data_fetcher` → `event_study` → `simulator` → ≥2 weeks
  PAPER → only then discuss LIVE with ~500 USDT and the conservative defaults.
- Realism check on latency: with a Basic X plan (polling) your tweet latency is
  seconds-to-tens-of-seconds. The event study tells you whether the move is
  still catchable at YOUR measured latency — believe the data, not hope.
- This system trades a violently mean-reverting niche with leverage available.
  The conservative defaults (5 USDT risk/trade, 25 USDT/day, 2x max leverage)
  exist so that being wrong is cheap. Raising them is your decision, not the
  bot's.

---

## Install

```bash
git clone <repo> && cd eliz-momentum-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill in tokens
pytest                    # 88 offline tests, no network needed
```

Or Docker:

```bash
docker compose up -d --build
docker compose logs -f bot
```

Restart safety (spec §20): on boot the bot restores daily risk state from
`daily_stats`, reloads open positions, reconciles them against the exchange
(`positionRisk`) and resolves UNKNOWN orders by `clientOrderId`. An exchange
position it doesn't recognize, or a DB position the exchange lost, **trips the
kill switch** instead of guessing. Duplicate orders are impossible by
construction: every order intent has a deterministic `newClientOrderId`.

## X API setup

1. Developer account at developer.x.com → create an app → **Bearer Token** →
   `X_BEARER_TOKEN`.
2. Access tiers (verify current limits in the portal — they change):
   - **Pro**: Filtered Stream works → set `X_LISTENER_MODE=stream` (or `auto`).
     Latency: sub-second to a few seconds.
   - **Basic**: no filtered stream. The bot auto-falls back to polling
     (`X_LISTENER_MODE=auto|poll`) every `X_POLL_INTERVAL_SECONDS` (default 8s;
     don't set it below what your tier's rate limit allows).
3. Every tweet's `twitter_latency_ms` is measured and stored — check it in the
   `tweets` table before believing any live-trading plan.

## Binance API setup

- PAPER: no keys needed at all (public market data only).
- LIVE: create keys at binance.com → API Management:
  - enable **Futures** only; **never enable withdrawals** (the bot calls no
    withdrawal endpoint and the key must not have the permission);
  - restrict to your server IP;
  - `BINANCE_API_KEY` / `BINANCE_API_SECRET` in `.env` (never in code/git).
- Futures **testnet** (recommended rehearsal before live):
  `BINANCE_FUTURES_TESTNET=true` routes all signed calls to
  `testnet.binancefuture.com` (keys from that site).

## Telegram

`@BotFather` → `/newbot` → `TELEGRAM_BOT_TOKEN`; message your bot once, get
your id from `@userinfobot` → `TELEGRAM_CHAT_ID`. You'll receive: NEW TWEET,
SIGNAL DETECTED, LONG/SHORT OPENED/CLOSED (with reference vs entry price,
move-before-entry, slippage, latency, PnL), SKIPPED (with reason), KILL SWITCH.

## Research pipeline (run this BEFORE any trading)

```bash
# 1. fetch ~3200 historical tweets + per-event futures aggTrades (ms precision)
python -m src.backtest.data_fetcher --max-tweets 3200 --horizon-min 35

# 2. the event study: peak/dump timing distributions per signal stage & phrase
python -m src.backtest.event_study --retrace 0.3 --stale-seconds 20
#    → data/event_study_report.json  (the "seconds until dump" answer lives here,
#       as median/p25/p75/p90 per definition — never a single average)
#    → data/phrase_edge.json          (per-phrase historical edge; the live
#       classifier loads this so "looks interesting" is scored by evidence)

# 3. causal replay through the REAL strategy code (same TradeSession as live)
python -m src.backtest.simulator --latency-s 3 --spread-bps 4
# try YOUR real latency: --latency-s 15 if you're on polling

# 4. performance report (LONG leg vs SHORT leg split — which side has the edge?)
python -m src.metrics
```

Honesty notes baked into the reports: aggTrades give **millisecond** trade
timestamps (that's the resolution claimed — nothing finer); events whose ticks
aren't available are **excluded, not approximated**; the D5 order-book dump
definition is reported `UNAVAILABLE_HISTORICALLY`; the simulator synthesizes
spread/book because historical books don't exist in aggTrades.

## Running

```bash
python -m src.main          # PAPER by default
```

State machine per signal:
`SIGNAL_DETECTED → MARKET_VALIDATION → ENTRY_APPROVED → LONG_OPEN → LONG_EXIT →
WAITING_SHORT_CONFIRMATION → SHORT_OPEN → SHORT_EXIT → DONE` (or
`SKIPPED`/`ABORTED`), every transition persisted to `strategy_events`.

Skip reasons recorded in `signals`: `TWEET_TOO_OLD`, `NOT_TRADE_SIGNAL`,
`SYMBOL_NOT_ON_BINANCE`, `PRICE_ALREADY_PUMPED`, `SPREAD_TOO_HIGH`,
`LOW_LIQUIDITY`, `RISK_LIMIT`, `LOW_CONFIDENCE`, `KILL_SWITCH`, `DUPLICATE`,
`DATA_STALE`, `EARLY_SIGNAL_DISABLED`.

## Configuration (all via `.env` — nothing hard-coded)

| Key | Default | Meaning |
|---|---|---|
| `MODE` | `PAPER` | `BACKTEST` / `PAPER` / `LIVE` |
| `ENABLE_LIVE_TRADING` | `false` | second live flag; both required |
| `MAX_TWEET_AGE_SECONDS` | 45 | older tweets never trade |
| `MAX_CHASE_PERCENT` | 1.5 | max move since tweet before we refuse to FOMO in |
| `MAX_SPREAD_PERCENT` | 0.10 | spread gate |
| `MIN_24H_VOLUME` | 20M | 24h quote-volume floor |
| `MIN_ORDERBOOK_LIQUIDITY` | 30k | top-5 notional per side |
| `MIN_CONFIDENCE` | 0.55 | classifier gate |
| `TRADE_EARLY_SIGNALS` | `false` | EARLY signals recorded only, until the event study justifies trading them |
| `MIN_REVERSAL_SCORE` | 65 | 0-100 behavior score to exit the long |
| `SHORT_CONFIRMATION_SECONDS` | 5 | reversal must SUSTAIN this long |
| `SHORT_CONFIRMATION_WINDOW_SECONDS` | 45 | no confirm in time ⇒ no short |
| `MAX_SHORT_HOLDING_SECONDS` | 900 | short leg time cap |
| `ACCOUNT_CAPITAL` | 500 | reference capital |
| `MAX_RISK_PER_TRADE_USDT` | 5 | stop-distance-based risk per trade |
| `MAX_DAILY_LOSS_USDT` | 25 | daily kill switch |
| `MAX_POSITION_NOTIONAL_USDT` | 100 | per-position notional cap |
| `MAX_LEVERAGE` | 2 | leverage ceiling |
| `MAX_TRADES_PER_DAY` | 6 | each leg counts (≈3 sessions/day) |
| `MAX_CONSECUTIVE_LOSSES` | 3 | streak kill switch |

Reversal weights/thresholds and long/short leg parameters are nested config
(`ReversalWeights`, `ReversalParams`, `LongParams`, `ShortParams` in
`src/core/config.py`) — tune them from event-study evidence, not vibes.

## Kill switch (spec §11)

Trips (blocking NEW trades; open positions keep being managed): daily loss,
max trades/day, consecutive losses, X feed problem, exchange API problem,
stale market data, excessive latency, unknown order state, unexpected/lost
exchange position, DB failure at restore. Fail-safe: unknown ⇒ NO TRADE.

## Tests

```bash
pytest      # unit + integration incl. full tweet→LONG→pump→reversal→SHORT flow
ruff check src tests
```

## Architecture

```
src/
  twitter/    listener (stream→poll fallback) · parser · hybrid classifier
  exchange/   fapi REST client · symbol mapper (filters) · per-session ws feed
  strategy/   entry filter · momentum tracker · reversal score · exits · session
  risk/       risk manager (sizing+limits) · kill switch
  execution/  paper & (double-flag) live adapters · order mgr · position mgr
  storage/    SQLAlchemy models (tweets/signals/market_snapshots/orders/trades/
              positions/strategy_events/daily_stats) · repo  [SQLite→Postgres]
  notifications/ telegram
  backtest/   data_fetcher · event_study (4 dump definitions) · simulator
  core/       config · logger · clock (Real/Sim) · state machine · domain
  main.py     orchestration
```

Notes recorded during development: `docs.x.com` and `developers.binance.com`
were unreachable from the build environment; Binance futures endpoints were
verified against the official `binance-futures-connector` source instead, and
X endpoint shapes follow API v2 — re-verify your tier's rate limits in the
developer portal before deployment.
