# Documentation index

Core documents live at the project root:

- [README](../README.md) — quick start & commands
- [ARCHITECTURE](../ARCHITECTURE.md) — system design, module map, data flow
- [SECURITY](../SECURITY.md) — execution safety model (production hard-disabled), secrets, trust boundaries
- [DATA_SOURCES](../DATA_SOURCES.md) — sources, reliability hierarchy, confirmation & decay
- [STRATEGY_RESEARCH](../STRATEGY_RESEARCH.md) — research loop, baselines, validation protocol
- [DECISIONS](../DECISIONS.md) — decision log (read first when changing anything)

## Operational runbook (short)

| Task | How |
|---|---|
| Start everything | `docker compose up -d` → http://127.0.0.1:8000/dashboard |
| Pause trading | Telegram `/pause`, `POST /api/v1/system/pause`, or set Redis key `system:kill` |
| Clear a risk lock | Telegram not enough by design — `POST /api/v1/system/risk-unlock` (deliberate operator action) |
| Replay a moment | `python -m app.research.replay --date 2026-01-01` |
| Backtest | `python -m app.research.backtest_cli --strategy trend_momentum --symbol BTCUSDT --walk-forward` |
| Fusion weight research | `python -m app.research.fusion_weights --symbol BTCUSDT` |
| Seed dashboard demo data | `python -m scripts.seed_demo` |
| DB migration | `alembic upgrade head` (runs automatically in the app container) |
