# STRATEGY_RESEARCH.md

## Research philosophy

1. Capital preservation beats trade frequency; no signal beats a low-quality signal.
2. Every beautiful backtest is guilty until proven innocent out-of-sample.
3. External information is evidence, not truth; Telegram is low-trust; LLM output is never an
   execution command.
4. Statistics, rules, market structure and risk management before any ML. Any future ML model must
   beat these baselines on identical walk-forward splits or it does not ship.

## Research loop (no stage may be skipped)

IDEA → BACKTEST → OUT-OF-SAMPLE TEST → STABILITY TEST → PAPER TRADING → PERFORMANCE REVIEW →
BINANCE TESTNET → PERFORMANCE REVIEW

Implemented as strategy lifecycle stages: `EXPERIMENTAL → BACKTESTED → VALIDATED → PAPER →
TESTNET`, with `DEGRADED` and `DISABLED` as demotion states. Promotion criteria live in
`app/strategies/promotion.py` and are configuration, with defaults documented in DECISIONS.md
(D-017). Every promotion/demotion is persisted as a system event with the metric values that
justified it.

## v1 research targets

Symbols: BTCUSDT, ETHUSDT, SOLUSDT (BNBUSDT collected but not traded by default).
Timeframes: signals on 1h primary, 15m confirmation, 4h/1d regime context. 1m/5m collected for
microstructure features and cost modeling.

### Baseline strategies (deliberately simple, interpretable)

1. **TrendMomentum v1** — EMA(50) > EMA(200) structure + positive 20-bar momentum + price above
   VWAP; eligible regimes: strong/weak uptrend; exits on structure break or ATR stop.
2. **VolumeBreakout v1** — Donchian(55) breakout confirmed by volume z-score > 2 and range
   expansion; eligible regimes: range → expansion transitions and trends; invalidation at
   breakout level.
3. **RangeMeanReversion v1** — Bollinger(20,2) lower-band touch with RSI(14) < 30 recovering,
   only in RANGE regime with normal volatility; target mid-band; hard ATR stop.

Each declares `required_features`, `eligible_regimes`, `risk_profile`, and is versioned;
parameters live in the strategy version record so results are attributable.

## Validation protocol

- Chronological train/validation/test splits; **walk-forward** with rolling windows
  (default: 180d train / 60d test, stepped by 60d) and a purge gap ≥ max feature lookback plus an
  embargo to prevent leakage across the split boundary.
- Costs always on: taker fee 10 bps/side (configurable), half-spread + impact slippage model,
  next-bar-open execution, exchange filters applied.
- Parameter sensitivity: ±20% grid around chosen parameters; a strategy whose profit factor
  collapses under perturbation is rejected (the "RSI 29 works, RSI 28/30 fail" red flag).
- Bootstrap resampling of trade sequences for drawdown/expectancy confidence intervals.
- Rejection rules (any → reject): works on only one asset, only one period, < 100 trades,
  negative expectancy after costs, in-sample-only performance, single-winner dependence
  (top trade > 40% of net profit), max DD > 20%.

## Scorecard (0–100)

Weighted, documented in `app/strategies/scorecard.py`: OOS risk-adjusted return 30, drawdown 15,
sample size 10, parameter stability 15, regime robustness 10, asset robustness 10, cost
sensitivity 5, recent degradation 5. Threshold to enter PAPER: ≥ 60 (configurable).

## Signal fusion research

Fusion weights are priors (equal within evidence groups; regime & data-quality multiplicative
gates). `app/research/fusion_weights.py` evaluates weight sets on walk-forward splits;
weight changes require a recorded research run — never hand-tuning against the full sample.

## Degradation monitoring

Rolling (30-trade / 14-day) expectancy, drawdown, losing streak, slippage vs model, signal
frequency, and feature distribution drift vs backtest expectations. Breach → strategy marked
DEGRADED: no new testnet entries, paper continues for observation, Telegram alert. Recovery or
retirement is a human research decision, not automatic re-tuning.
