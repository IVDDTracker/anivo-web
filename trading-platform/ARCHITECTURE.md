# ARCHITECTURE.md

QuantLab is a modular crypto market-intelligence and algorithmic-trading **research** platform.
It collects market and external data, normalizes it, generates and confirms signals, backtests,
paper-trades, optionally trades on **Binance Spot Testnet**, and reports through Telegram and a
local dashboard. **Production order execution is hard-disabled by construction** (see SECURITY.md).

## System overview

```
                 ┌────────────────────────────────────────────────────────────┐
                 │                        COLLECTORS                          │
                 │  Binance WS/REST · Telegram ingest · GitHub · RSS/News     │
                 └───────────────┬────────────────────────────────────────────┘
                                 │ RawEvent (persisted first — replayable)
                                 ▼
                 ┌───────────────────────────────┐
                 │  NORMALIZATION + DEDUP        │  event_hash, source scoring
                 └───────────────┬───────────────┘
                                 │ MarketEvent / ExternalEvent
                 ┌───────────────▼───────────────┐     ┌──────────────────────┐
                 │  EVENT INTELLIGENCE           │◄────┤ SOURCE RELIABILITY   │
                 │  decay · confirmation · dupe  │     │ ENGINE               │
                 └───────────────┬───────────────┘     └──────────────────────┘
                                 │ evidence
     ┌──────────────┐   ┌────────▼────────┐   ┌─────────────────┐
     │ FEATURE      │──▶│ SIGNAL ENGINE   │◄──│ REGIME ENGINE   │
     │ ENGINE       │   │ strategies +    │   │ (interpretable) │
     └──────────────┘   │ fusion          │   └─────────────────┘
                        └────────┬────────┘
                                 │ Signal (all persisted, incl. rejected)
                                 ▼
                 ┌───────────────────────────────┐
                 │        DECISION PIPELINE      │  every stage returns an
                 │ data quality → confirmation → │  auditable StageResult
                 │ strategy → regime → RISK →    │
                 │ portfolio → exec simulation → │
                 │ FINAL DECISION                │
                 └───────┬───────────────┬───────┘
                         │approved       │rejected (persisted with reasons)
              ┌──────────▼─────┐  ┌──────▼───────┐
              │ PAPER ENGINE   │  │ audit trail  │
              │ (always)       │  └──────────────┘
              └──────┬─────────┘
                     │ strategy in TESTNET stage only
              ┌──────▼─────────┐        ┌──────────────────────────────┐
              │ TESTNET        │        │ PRODUCTION EXECUTOR          │
              │ EXECUTOR       │        │ raises                       │
              │ (Binance Spot  │        │ ProductionExecutionDisabled  │
              │  Testnet)      │        │ — always, no bypass          │
              └──────┬─────────┘        └──────────────────────────────┘
                     │
      ┌──────────────┴───────────────────────────────────────────┐
      │ Telegram notify · FastAPI dashboard · Prometheus metrics │
      │ daily/weekly reports · replay · scorecards               │
      └──────────────────────────────────────────────────────────┘
```

## Module map (`app/`)

| Module | Responsibility |
|---|---|
| `config/` | Pydantic settings, YAML source configs, asset universe, limits |
| `core/` | clock, event bus, state machine, logging, hashing, errors, task supervisor |
| `data/collectors/` | plug-in collectors (Binance WS/REST, Telegram, GitHub, RSS) + health |
| `data/normalization/` | raw payload → typed events, dedup |
| `features/` | technical + microstructure feature computation |
| `regimes/` | interpretable market regime classifier |
| `signals/` | Signal model, evidence, fusion (meta-signal) |
| `strategies/` | strategy plug-ins, registry, lifecycle stages, scorecard, degradation |
| `risk/` | independent risk engine with absolute veto; limits, locks, kill switch |
| `portfolio/` | correlation, exposure aggregation, position sizing |
| `backtest/` | event-driven backtester, cost models, metrics, walk-forward validation |
| `paper/` | realistic internal execution simulator + ledger |
| `execution/` | order intents, symbol filters, testnet executor, disabled production executor, reconciliation |
| `pipeline.py` | the DecisionPipeline wiring all gates |
| `models/` | Pydantic domain models (single source of truth for shapes) |
| `storage/` | SQLAlchemy models, repositories, Redis access, retention |
| `telegram/` | control bot (commands), notifier, ingestion |
| `monitoring/` | health service, Prometheus metrics, degradation detection |
| `reports/` | daily/weekly report builders |
| `research/` | replay CLI, walk-forward harness, fusion-weight evaluation |
| `api/` | FastAPI routes + dashboard page |

## Key design rules

1. **Single writer path to orders.** Only `DecisionPipeline.decide()` can produce an
   `ApprovedIntent`; only executors accept `ApprovedIntent`. Collectors, LLM output, Telegram and
   strategies have no reference to executors.
2. **Fail-safe.** Every gate must affirmatively pass. Missing data, stale data, DB/Redis errors,
   state mismatches → DO_NOTHING with a persisted reason. No default-allow branches exist.
3. **Risk engine is sovereign.** It is constructed independently of strategies, evaluated after
   them, and its veto is final. Strategies cannot see or mutate risk state.
4. **Auditability.** Signals (accepted and rejected), stage results, risk events, orders, fills,
   regime changes and system events are persisted with reasons and evidence.
5. **Replayability.** Raw events are persisted before processing. `python -m app.research.replay
   --date YYYY-MM-DD` reconstructs the platform's knowledge as-of that time using `SimClock`.
6. **Clock injection.** No decision code calls "now" directly; it asks the injected `Clock`.
   This structurally prevents lookahead in backtests and replay.
7. **Crash isolation.** Every collector/service runs under a supervisor with exponential-backoff
   restart; component failure degrades the system state instead of killing the process.

## System states

`STARTING → HEALTHY ⇄ DEGRADED ⇄ DATA_STALE`, plus `PAUSED` (operator), `RISK_LOCK` (risk engine),
and execution modes `PAPER_ONLY` / `TESTNET_ACTIVE`. New positions require: state ∈ {HEALTHY} and
not PAUSED/RISK_LOCK and market data fresh for the target symbol. Exits/closes are allowed in
DEGRADED to avoid trapping positions, and never blocked by pause.

## Data flow timing

- Market ticks (book ticker/trades) update Redis hot state and rolling in-memory windows.
- Candle closes trigger feature computation → regime update → strategy evaluation → pipeline.
- External events flow continuously into the event store; they influence signals only through the
  event-intelligence score of each symbol (decayed, confirmation-weighted).
- The pipeline runs per (symbol, timeframe) on candle close; there is no tick-level trading in v1.

## Persistence

PostgreSQL via SQLAlchemy 2 async; Alembic migrations. Tables: see `app/storage/tables.py`
(market_candles, market_events, trades, orderbook_snapshots, external_events, sources,
source_health, features, signals, signal_evidence, strategies, strategy_versions, backtests,
backtest_trades, paper_orders, paper_fills, paper_positions, testnet_orders, testnet_fills,
testnet_positions, risk_events, regime_history, system_events, performance_snapshots,
trade_intents, decision_records). Redis holds only reconstructible hot state.

## Deployment

`docker compose up -d` starts `postgres`, `redis`, and `app` (FastAPI + all supervised tasks).
The app runs Alembic migrations on start, restores open paper/testnet state from Postgres,
reconciles testnet orders, and enters STARTING → HEALTHY once data is flowing.
