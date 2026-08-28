# eliz-momentum-bot

Event-driven research & trading system for the hypothesis:

> **SIGNAL MESSAGE (Telegram channel / tweet) → audience reaction → short-term
> buying pressure → local peak → mean reversion.**

**Default configuration (v2 pivot): Telegram channels as the free signal
source + SHORT_ONLY strategy.** The bot never enters the pump; it waits for a
real pump to print on the exchange (≥ `MIN_PUMP_PERCENT` vs the message-time
price) and a *sustained, confirmed* reversal, then shorts the fade with strict
SL/TP/trailing. This kills both weaknesses of the original design: the X API
cost (Pro streaming ≈ $5k/mo) and the sub-second latency race — the reversal
side is timed in tens of seconds, not milliseconds.

Rules of engagement: the account only reads channels YOU joined; messages are
data, never instructions; and this bot must never be used to join or
coordinate pumps — it observes public messages and trades the aftermath with
public market data. Shorting a pump's collapse bets *against* manipulation,
not with it.

How it works: configured Telegram channels (and/or `@eliz883` on X) are
watched in real time; messages are classified (EARLY vs CONFIRMED signal),
market conditions are validated on Binance USDⓈ-M Futures, and the trade logic
runs per `STRATEGY_MODE`. In `LONG_SHORT` (legacy) the bot rides the pump
first; in `SHORT_ONLY` (default) it only fades the confirmed reversal, using
the same behavior-based **reversal score** (never a fixed timer). Everything —
messages, signals, skips, orders, PnL, latencies, state transitions — is
persisted; the Telegram bot reports every step.

**This system does not assume the signal poster is a good trader — it measures
what their audience does to price, first, and only then trades it** (core
principle: measure before trading; uncertainty ⇒ NO TRADE).

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
pytest                    # 104 offline tests, no network needed
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

## Telegram source setup (default, free)

1. https://my.telegram.org → API development tools → `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`.
2. `python -m src.telegram_source.login` → interactive phone login → paste the
   printed `TELEGRAM_SESSION` into `.env` (it's a credential — keep it secret).
3. Join the channels you want to monitor **with that account**, then list them:
   `TG_SOURCE_CHANNELS=@channel1,@channel2`.
4. Message latency (post → bot) is typically 1-3s and is measured per message.

Research pipeline for Telegram history (free, no X account needed):

```bash
python -m src.backtest.tg_history --limit 2000   # channel history + aggTrades
python -m src.backtest.event_study               # pump/dump distributions
python -m src.backtest.simulator                 # SHORT_ONLY causal replay
python -m src.metrics                            # short-leg performance
```

**Do not skip this.** If the event study says pumps in your channels don't
retrace reliably after costs, the strategy has no edge — that's a result, not
a failure.

### SHORT_ONLY flow

`SIGNAL_DETECTED → MARKET_VALIDATION → MONITORING_PUMP →
WAITING_SHORT_CONFIRMATION → SHORT_OPEN → SHORT_EXIT → DONE`

- MARKET_VALIDATION: spread/volume/liquidity/staleness gates (the chase filter
  is intentionally OFF — a pump having happened is the point).
- MONITORING_PUMP: needs peak gain ≥ `MIN_PUMP_PERCENT` AND reversal score ≥
  `MIN_REVERSAL_SCORE` within `PUMP_WATCH_WINDOW_SECONDS`, else DONE (no trade).
- Confirmation: score must SUSTAIN `SHORT_CONFIRMATION_SECONDS` below VWAP with
  a bounce veto — never short into strength; a resuming pump hits a tight SL.
- **Never short obvious real news** (listings etc.): the classifier tags them,
  and the bearish/short filters plus tight SL bound the damage — but review
  skips/PnL per channel in the DB and drop channels that post news, not pumps.

## X API setup (only if SIGNAL_SOURCE includes X)

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
