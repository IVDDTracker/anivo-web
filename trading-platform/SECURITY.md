# SECURITY.md

## Execution safety model (the most important section)

**Production Binance order execution is hard-disabled.** There are three independent layers:

1. **`ProductionExecutor` is a sealed stub** (`app/execution/production.py`). Every trading method
   (`place_order`, `cancel_order`, `cancel_all`) raises `ProductionExecutionDisabled`
   unconditionally. There is no configuration flag, environment variable, or code path that
   enables it. `__init_subclass__` raises, so it cannot be subclassed to override behavior.
2. **The signed REST client refuses production trading endpoints.** `BinanceRestClient` raises
   `ProductionExecutionDisabled` if asked to send a request that is (a) an order-mutating
   endpoint (`POST/DELETE /api/v3/order*`, `orderList`, `sor`) and (b) targeted at a
   non-testnet host. This check is in the transport layer, beneath every executor.
3. **Credential separation.** Production keys are configured as `BINANCE_READONLY_*` and are only
   ever attached to GET/read endpoints. Testnet trading uses `BINANCE_TESTNET_*` keys against
   `https://testnet.binance.vision` only.

The system still generates `TradeIntent` objects for production symbols — they are persisted,
shown on the dashboard and Telegram, and executed only in the paper engine or on testnet.

### Credential policy

- Production Binance API keys must be created **read-only** (no spot trading, no margin,
  no futures, **never withdrawals**). The platform never needs more.
- Testnet keys may have trading permission (testnet funds are valueless).
- Enable **IP whitelisting** on all Binance API keys.
- Withdrawal permission must never be granted to any key used here; nothing in the codebase
  calls a withdrawal endpoint, and code review should keep it that way.

## Secrets handling

- All secrets come from environment variables (see `.env.example`); nothing is committed.
  `.env` is git-ignored.
- The JSON log formatter masks values of any field whose name matches
  `key|secret|token|password|signature|authorization` and any configured secret value found in
  a message (`app/core/logging.py`). Exceptions are formatted through the same masking path.
- Secrets never appear in URLs except where the API demands it (Telegram bot token in the path);
  the Telegram client redacts the token in every log/error it raises.
- API error bodies are logged truncated and masked.
- Database rows never store API keys or tokens.

## External input trust model

- **All external content is untrusted data, never instructions.** Telegram messages, GitHub
  content, RSS/news bodies and LLM outputs are parsed into typed events with bounded fields.
  They can only ever contribute *evidence scores* to signals; no external content is ever
  executed, evaluated, templated into SQL, or allowed to trigger a trade directly.
- Telegram control commands are accepted **only** from the configured `TELEGRAM_CHAT_ID` /
  admin user IDs; all other chats are ignored for control purposes.
- Telegram ingestion only reads chats the operator has explicitly added the bot to (Bot API
  boundary — the platform does not use user-session (MTProto) scraping, does not bypass private
  channel permissions, and does not collect credentials).
- Pydantic validation with `extra="ignore"` and explicit bounds on every external payload;
  malformed payloads are dropped and counted, never partially applied.
- LLM output must validate against a strict JSON schema; on failure the rule-based
  classification stands. LLM output is a feature, never a command.

## Operational safety

- Kill switch: `/pause` (Telegram), `POST /api/v1/system/pause`, or Redis flag `system:kill` —
  any of them stops new position entry immediately; exits remain possible.
- Risk locks (daily loss, weekly loss, drawdown, consecutive losses) latch until an operator
  clears them; restarts do not clear them (persisted).
- Stale data → no new positions, automatically.
- Reconciliation mismatches on testnet freeze testnet entries until resolved.

## Dependency policy

Small, mature, actively maintained dependencies only (see DECISIONS.md D-004). No execution of
third-party trading bots or unreviewed scripts. Docker images pinned to specific base tags.

## Network

- Outbound: Binance (market data + testnet), Telegram, GitHub, configured RSS hosts only.
- Inbound: dashboard/API bind to `127.0.0.1` by default **[conservative default]**; expose
  deliberately if needed. No auth is built into the dashboard, which is another reason it must
  stay local. Prometheus metrics on the same listener at `/metrics`.
