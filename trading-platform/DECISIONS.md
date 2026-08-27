# DECISIONS.md — Technical Decision Log

Every material technical decision is recorded here with its rationale. Newest entries at the bottom
of each section. Decisions marked **[DEFAULT]** were unspecified in the requirements and use a
conservative default that can be changed via configuration.

---

## D-001: Repository placement

The host repository (`anivo-web`) is a static website. The trading platform is developed as a fully
self-contained project under `trading-platform/` on a dedicated branch so it never interferes with
the website's files or deploy config. All paths in this document are relative to `trading-platform/`.

## D-002: Language / runtime

Python 3.12 in Docker (official `python:3.12-slim` image). Code is kept 3.11-compatible
(`requires-python >= 3.11`) because the current CI/dev container ships 3.11; no 3.12-only syntax is
used. asyncio end to end; blocking work (pandas/numpy analytics) is bounded and executed in
short bursts or thread offload where needed.

## D-003: Verified API facts (Phase 1 research)

Researched from the **official** `binance/binance-spot-api-docs` GitHub repository
(canonical source for developers.binance.com), 2026-08-27:

### Binance Spot REST
- Base endpoints: `https://api.binance.com` (plus api1–api4, api-gcp).
- **Public market data only**: `https://data-api.binance.vision` — used by default for market data
  so no API key is consumed and prod credentials remain untouched for public data.
- Rate limits are **weight-based** and defined dynamically in `GET /api/v3/exchangeInfo`
  (`rateLimits` array: `REQUEST_WEIGHT`, `ORDERS`, `RAW_REQUESTS`). We read the limits at startup
  instead of hard-coding them, track `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` response
  headers, honor `Retry-After` on HTTP 429/418, and back off. Limits are per-IP.
- HTTP 5XX and `-1007 TIMEOUT` mean **execution status UNKNOWN** — never treat as failure;
  reconcile by querying order status before any retry. This drives the executor design (D-013).
- SIGNED endpoints: HMAC SHA256 of the query string, `timestamp` + optional `recvWindow`
  (max 60000 ms). We default `recvWindow=5000`. **[DEFAULT]**
- `newClientOrderId`: client-generated, unique among open orders → used as the idempotency key.

### Binance Spot WebSocket
- Market data: `wss://stream.binance.com:9443` (or `:443`); market-data-only endpoint
  `wss://data-stream.binance.vision` — used by default (no user data needed on that connection).
- A single connection is valid **24 hours max** → collector proactively reconnects before expiry.
- Server sends a **ping frame every 20 s**; client must pong within 1 minute or is disconnected.
  The `websockets` library auto-answers pings; we additionally monitor last-message age.
- Limits: 5 incoming messages/s, 1024 streams/connection, 300 connection attempts per 5 min/IP.
- Combined streams (`/stream?streams=a/b/c`) wrap payloads in `{"stream": ..., "data": ...}`.
- `serverShutdown` event may be sent before shutdown → treat as reconnect-now signal.

### Binance Spot Testnet
- Base endpoint: `https://testnet.binance.vision/api`; WS: `wss://stream.testnet.binance.vision`.
- Same API surface as production Spot; keys created at https://testnet.binance.vision.
- Testnet data/balances are periodically reset by Binance → reconciliation must tolerate a
  full state wipe (treated as "close all local open testnet state with reason RESET").

### Symbol filters (from official `filters.md`)
- `PRICE_FILTER`: `price % tickSize == 0`, min/max price (tickSize==0 → disabled).
- `LOT_SIZE`: `quantity % stepSize == 0`, minQty/maxQty. `MARKET_LOT_SIZE` for MARKET orders.
- `NOTIONAL` / `MIN_NOTIONAL`: `price * quantity >= minNotional`, with `applyMinToMarket` /
  `applyToMarket` flags; for market orders the average price is used.
- All rounding is **down** (floor to step) and implemented in `Decimal`, never float (D-012).

### Telegram Bot API
- Official HTTP API `https://api.telegram.org/bot<token>/<method>`. We use **long polling**
  (`getUpdates`, timeout=50) — no public inbound webhook needed for a local deployment. **[DEFAULT]**
- Message sends are rate limited (~30 msg/s overall, 1 msg/s per chat sustained); the notifier
  queues and throttles, and collapses repeated alerts.
- A bot only receives messages from chats it is a member of and (in groups) according to its
  privacy mode. Channel ingestion therefore only works for channels where the bot is added as
  an admin/member by the owner — this is the legal/technical authorization boundary. We do not
  use MTProto user-session scraping. See SECURITY.md.

### GitHub REST API
- `https://api.github.com`, version header `X-GitHub-Api-Version: 2022-11-28`,
  `Accept: application/vnd.github+json`. 5000 req/h with token, 60/h anonymous.
- We poll releases/commits/advisories with **ETag** conditional requests (304s are free for the
  core rate limit budget in most cases and cheap regardless) on a slow cadence (default 5 min).

## D-004: Framework and library choices

- **FastAPI + uvicorn**: API/dashboard/health. Mature, typed, Pydantic-native.
- **Pydantic v2 + pydantic-settings**: domain models, config, and validation of all external input.
- **SQLAlchemy 2 (async) + asyncpg + Alembic**: persistence and migrations. `aiosqlite` for tests.
- **redis-py (async)**: hot state (latest prices, health heartbeats, kill-switch flag).
- **httpx**: REST clients (Binance, Telegram, GitHub, RSS). One client class per provider.
- **websockets**: Binance stream client.
- **numpy + pandas**: features/backtesting. **Polars and scikit-learn intentionally deferred**
  [DEFAULT]: no current workload needs them; adding them now violates the "no unnecessary
  dependencies / no fake AI" rules. The feature engine is columnar and can be ported to Polars
  if profiling shows need.
- **No Binance SDK**: the official `binance-connector` is a thin REST wrapper; writing our own
  ~300-line signed client gives us precise control of retry/idempotency/rate-limit behavior that
  is safety-critical here, with fewer supply-chain dependencies. Same reasoning for Telegram
  (plain Bot API over httpx instead of aiogram) and GitHub (REST + ETag over httpx instead of
  PyGithub). All three APIs are stable, documented HTTP APIs.
- **RSS parsing with stdlib `xml.etree`**: avoids `feedparser` (large, sync, historically slow to
  patch). We only need title/link/date/summary from well-formed feeds; malformed feeds are
  dropped and the source marked unhealthy.
- **prometheus-client**: metrics; **jinja2**: report templating; dashboard is a static single-file
  HTML page consuming the JSON API (no JS build chain).
- **hypothesis**: property-based tests for rounding/sizing/PnL invariants; **respx** for httpx mocks.

## D-005: Process model

Single deployable app process (`app.main`) supervising asyncio tasks: collectors, pipeline,
paper engine, telegram bot, monitors, plus the FastAPI server. A separate process would add IPC
complexity without benefit at this scale. Postgres and Redis run as separate containers.
Every supervised task is wrapped in a crash-guard that logs, marks the component unhealthy, and
restarts with exponential backoff — a dying collector never brings down the system.

## D-006: Event flow

In-process async pub/sub bus (`app/core/bus.py`) with bounded queues per subscriber
(backpressure: oldest-dropped for market ticks, blocking for decision-critical events).
Raw events are persisted for replay before normalization. Kafka/NATS rejected as overkill.

## D-007: Time

All internal timestamps are timezone-aware UTC `datetime`. A `Clock` abstraction is injected
everywhere decisions are made; `RealClock` in live mode, `SimClock` in backtest/replay — this is a
structural defense against lookahead (code cannot ask "what time is it really" during a backtest).

## D-008: Trading scope

SPOT only. LONG-only position model in v1 **[DEFAULT]** — spot shorting requires margin, which is
explicitly banned. Strategies may emit SHORT signals; they are recorded, shown, and usable as
"exit/close" or research evidence, but the risk engine rejects short *entries* on spot.
No leverage, no margin, no futures, no martingale/averaging-down, no DCA loops.

## D-009: Production execution is hard-disabled

`ProductionExecutor` exists only as a sealed stub: every trading method raises
`ProductionExecutionDisabled` unconditionally — there is no flag, env var, or subclass hook that
enables it (`__init_subclass__` blocks overriding). The Binance REST client refuses to sign
order-placing requests against production hosts as a second, independent layer. See SECURITY.md.

## D-010: Signal pipeline order (the only path to an order)

DATA QUALITY → SIGNAL GENERATION → CONFIRMATION → STRATEGY FILTER → REGIME FILTER →
RISK ENGINE → PORTFOLIO FILTER → EXECUTION SIMULATION → FINAL DECISION.
Implemented as an explicit `DecisionPipeline` where each stage returns an auditable
`StageResult(passed, reasons, evidence)` that is persisted with the signal. External events, LLM
output, Telegram messages etc. can only enter as *features/evidence* of stage 2–4; nothing else
can construct an order intent.

## D-011: Fail-safe defaults

Any uncertainty (stale data, DB error, Redis down, reconcile mismatch, missing feature, risk engine
unavailable) → the pipeline returns DO_NOTHING and records why. There is no "default allow"
anywhere in the decision path; every gate must affirmatively pass.

## D-012: Numerics

`Decimal` for everything that leaves the system towards an exchange (prices, quantities, filter
rounding, notional checks) and for money accounting in paper/testnet ledgers. `float`/numpy for
statistical features and backtest analytics where 1e-12 error is irrelevant. Conversion happens at
one boundary (`app/execution/filters.py`).

## D-013: Order idempotency & unknown outcomes

`newClientOrderId` is deterministic: `ql-<sha256(intent_id)[:24]>` derived from the persisted
intent UUID. Flow: persist intent (status=PENDING_SUBMIT) → send → on timeout/5xx/-1007 the status
becomes UNKNOWN and the reconciler queries by `origClientOrderId` before any resend. A resend uses
the *same* clientOrderId, so a duplicate is rejected by the exchange rather than double-executed.

## D-014: Signal fusion weights

Initial component weights are explicit, config-visible **research priors**, not tuned constants:
equal-weighted within evidence groups, with regime/data-quality acting multiplicatively as gates
rather than additive terms. The research module ships a walk-forward weight evaluation harness;
weights may only change through a recorded research run (see STRATEGY_RESEARCH.md). We do not
pretend the priors are optimal, and the scorecard treats fused signals like any other strategy.

## D-015: Regime classification

Interpretable statistics only (EMA structure/slope, realized-vol percentile vs its own history,
Donchian range position, drawdown speed for panic detection, volume/liquidity floor). No HMM/ML in
v1 by design; the interface allows a future classifier behind the same enum.

## D-016: Retention **[DEFAULT]**

Raw high-frequency events (trades/book tickers/depth): 14 days. 1m candles: 400 days. ≥1h candles,
signals, decisions, orders, fills, positions, reports: indefinite. Order book snapshots: 7 days.
Enforced by a retention job (daily); values configurable in `app/config`.

## D-017: Strategy promotion thresholds **[DEFAULT]**

Documented defaults, all configurable and recorded per-decision: min 100 backtest trades, min 30
out-of-sample trades, profit factor ≥ 1.2 OOS, max drawdown ≤ 20%, expectancy > 0 after costs,
parameter stability (±20% parameter perturbation keeps profit factor ≥ 1.0), ≥ 14 days paper with
performance within 1.5σ of backtest expectation before TESTNET. Rationale: large enough samples to
reject luck at ~95% confidence for plausible edge sizes, small enough to be reachable. These are
starting points for research, not truth.

## D-018: LLM usage

Optional and disabled by default. If enabled (Claude API key present), it is used only for
classification/extraction/summarization of external events into a strict Pydantic schema
(`app/models/llm.py`); malformed output is rejected and the event keeps its rule-based
classification. LLM output can never reach the executor: it only writes `ExternalEvent` fields,
which enter the pipeline as evidence. The default pipeline runs fully without any LLM.

## D-019: Dashboard

Server-rendered single-page HTML + fetch() against `/api/v1/*`. No React/build chain: the dashboard
is a local operations tool; keeping it dependency-free beats aesthetics. All panels are read-only.

## D-020: Testing without live services

Unit/integration tests run against SQLite (aiosqlite) and fakes for Redis/HTTP (respx), so the full
suite runs in CI with no network. Chaos tests simulate disconnects, duplicate/out-of-order WS
messages, price anomalies, and submit-timeouts. Anything touching real Binance testnet is behind
`scripts/` and requires explicit credentials.

## D-021: Environment constraint noted during development

The build sandbox's egress proxy blocks `api.binance.com`/`testnet.binance.vision`, so live
connectivity was not exercised here; correctness is covered by contract tests written against the
official documented payloads (fixtures in `tests/fixtures/`).
