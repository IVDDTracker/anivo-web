# QuantLab — Crypto Market Intelligence & Trading Research Platform

A modular, production-quality research platform that collects Binance market data and external
intelligence (Telegram, GitHub, RSS/news), computes features, detects market regimes, generates and
fuses signals, backtests with walk-forward validation, paper-trades realistically, optionally
executes on **Binance Spot Testnet**, and reports through Telegram and a local dashboard.

> **Safety first: real-money order execution is HARD-DISABLED.**
> The production executor always raises `ProductionExecutionDisabled`; the REST transport
> independently refuses order endpoints on production hosts; production keys are read-only.
> See [SECURITY.md](SECURITY.md).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system design and module map
- [SECURITY.md](SECURITY.md) — execution safety model, secrets, trust boundaries
- [DATA_SOURCES.md](DATA_SOURCES.md) — sources, reliability hierarchy, confirmation/decay
- [STRATEGY_RESEARCH.md](STRATEGY_RESEARCH.md) — research loop, baselines, validation protocol
- [DECISIONS.md](DECISIONS.md) — technical decision log (read this first)

## Quick start

```bash
cd trading-platform
cp .env.example .env        # fill in what you have; everything is optional except DB/Redis
docker compose up -d        # postgres + redis + app
open http://127.0.0.1:8000/dashboard
```

Without any API keys the platform still runs: public Binance market data needs no key.
Telegram/GitHub/testnet features activate when their credentials are present.

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # full offline test suite (SQLite + mocked HTTP)
ruff check app tests
python -m app.main          # needs Postgres+Redis (docker compose up -d postgres redis)
```

## Useful commands

```bash
python -m app.research.replay --date 2026-01-01      # replay knowledge as-of a date
python -m app.research.backtest_cli --strategy trend_momentum --symbol BTCUSDT
python -m scripts.seed_demo                          # seed demo data for the dashboard
```

## Telegram bot commands

`/status /signals /positions /performance /risk /sources /pause /resume /report /health`

## Core principles

1. Capital preservation beats trade frequency.
2. No signal is better than a low-quality signal.
3. Backtests are not trusted until validated out-of-sample.
4. External information is evidence, not truth; Telegram is low-trust; LLM output is never a command.
5. The risk engine has absolute veto power.
6. Every decision is auditable; rejected signals are persisted with reasons.
7. Production order execution remains disabled. No bypass exists.
