"""Backtest engine tests: execution mechanics, costs, no-lookahead, metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.backtest.engine import BacktestConfig, Backtester
from app.backtest.metrics import compute_metrics
from app.backtest.walkforward import make_windows
from app.config.settings import CostConfig
from app.models.enums import Direction
from app.models.market import Candle
from app.models.signals import Signal
from app.strategies.base import BaseStrategy, StrategyContext
from tests.helpers import make_candles

START = datetime(2025, 1, 1, tzinfo=UTC)


def flat_candles(n: int, price: float = 100.0, **overrides) -> list[Candle]:
    out = []
    for i in range(n):
        open_time = START + timedelta(hours=i)
        values = dict(open=price, high=price * 1.001, low=price * 0.999, close=price)
        values.update({k: v(i) if callable(v) else v for k, v in overrides.items()})
        out.append(Candle(symbol="BTCUSDT", timeframe="1h", open_time=open_time,
                          close_time=open_time + timedelta(hours=1), volume=100,
                          quote_volume=100 * price, trades=10, taker_buy_base=50, **values))
    return out


class ScriptedStrategy(BaseStrategy):
    """Deterministic entries at configured bar indices; optional stops/targets."""

    name = "scripted"
    version = "1.0"

    @classmethod
    def default_params(cls) -> dict:
        return {"entry_indices": (), "stop_pct": None, "target_pct": None}

    def __init__(self, **params):
        super().__init__(**params)
        self.seen_history_lengths: list[int] = []
        self.seen_last_close_time: list[datetime] = []

    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        self.seen_history_lengths.append(len(ctx.candles))
        self.seen_last_close_time.append(ctx.candles[-1].close_time)
        idx = len(ctx.candles) - 1
        if idx not in self.params["entry_indices"]:
            return None
        stop = ctx.close * (1 - self.params["stop_pct"] / 100) if self.params["stop_pct"] else None
        target = ctx.close * (1 + self.params["target_pct"] / 100) if self.params["target_pct"] else None
        return self._base_signal(ctx, Direction.LONG, confidence=80, stop=stop, target=target,
                                 evidence=[], risks=[])


def _cfg(**kw) -> BacktestConfig:
    defaults = dict(initial_equity=10_000.0, risk_pct_per_trade=1.0, max_notional_pct=50.0,
                    warmup_bars=60, costs=CostConfig(taker_fee_bps=10, base_slippage_bps=2))
    defaults.update(kw)
    return BacktestConfig(**defaults)


class TestExecutionMechanics:
    def test_entry_fills_at_next_bar_open_plus_slippage(self):
        candles = flat_candles(80)
        candles[66] = candles[66].model_copy(update={"open": 100.0})
        strat = ScriptedStrategy(entry_indices=(65,), stop_pct=5, target_pct=None)
        result = Backtester(_cfg()).run(strat, candles)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_time == candles[66].open_time
        expected_fill = 100.0 * (1 + 2 / 10_000)
        assert abs(trade.entry_price - expected_fill) < 1e-9

    def test_stop_hit_fills_at_stop_with_slippage(self):
        candles = flat_candles(90)
        # bar 68 dips below the stop (entry at 66: stop = ~100*(0.98))
        candles[68] = candles[68].model_copy(update={"low": 90.0, "open": 100.0, "close": 99.0})
        strat = ScriptedStrategy(entry_indices=(66,), stop_pct=2)
        result = Backtester(_cfg()).run(strat, candles)
        trade = result.trades[0]
        assert trade.exit_reason == "stop"
        stop_level = trade.entry_price / (1 + 2 / 10_000) * 0.98  # stop is 2% below signal close
        assert trade.exit_price < stop_level  # slippage makes it worse than the stop

    def test_gap_through_stop_fills_at_open_not_stop(self):
        candles = flat_candles(90)
        candles[68] = candles[68].model_copy(update={"open": 80.0, "low": 79.0, "close": 80.0,
                                                    "high": 81.0})
        strat = ScriptedStrategy(entry_indices=(66,), stop_pct=2)
        result = Backtester(_cfg()).run(strat, candles)
        trade = result.trades[0]
        assert trade.exit_reason == "stop"
        assert trade.exit_price < 81.0  # filled near the gap open, NOT at the stop level

    def test_stop_and_target_same_bar_assumes_stop_first(self):
        candles = flat_candles(90)
        candles[68] = candles[68].model_copy(update={"low": 90.0, "high": 120.0, "open": 100.0})
        strat = ScriptedStrategy(entry_indices=(66,), stop_pct=2, target_pct=5)
        result = Backtester(_cfg()).run(strat, candles)
        assert result.trades[0].exit_reason == "stop"

    def test_target_hit(self):
        candles = flat_candles(90)
        candles[70] = candles[70].model_copy(update={"high": 120.0, "open": 100.0})
        strat = ScriptedStrategy(entry_indices=(66,), stop_pct=10, target_pct=5)
        result = Backtester(_cfg()).run(strat, candles)
        trade = result.trades[0]
        assert trade.exit_reason == "target"
        assert trade.pnl > 0

    def test_fees_and_slippage_reduce_pnl(self):
        candles = flat_candles(90)
        candles[70] = candles[70].model_copy(update={"high": 120.0, "open": 100.0})
        cheap = Backtester(_cfg(costs=CostConfig(taker_fee_bps=0, base_slippage_bps=0))).run(
            ScriptedStrategy(entry_indices=(66,), stop_pct=10, target_pct=5), candles)
        costly = Backtester(_cfg(costs=CostConfig(taker_fee_bps=20, base_slippage_bps=10))).run(
            ScriptedStrategy(entry_indices=(66,), stop_pct=10, target_pct=5), candles)
        assert costly.trades[0].pnl < cheap.trades[0].pnl
        assert costly.trades[0].fees > 0 and costly.trades[0].slippage > 0

    def test_round_trip_pnl_accounting_matches_equity(self):
        candles = flat_candles(120)
        candles[70] = candles[70].model_copy(update={"high": 120.0, "open": 100.0})
        cfg = _cfg()
        result = Backtester(cfg).run(
            ScriptedStrategy(entry_indices=(66,), stop_pct=10, target_pct=5), candles)
        final_equity = result.equity_curve[-1][1]
        assert abs(final_equity - (cfg.initial_equity + sum(t.pnl for t in result.trades))) < 1e-6

    def test_position_sizing_risk_based(self):
        candles = flat_candles(90)
        cfg = _cfg()
        result = Backtester(cfg).run(ScriptedStrategy(entry_indices=(66,), stop_pct=2), candles)
        trade = result.trades[0]
        risk_capital = cfg.initial_equity * cfg.risk_pct_per_trade / 100
        stop_level = 100.0 * 0.98  # signal close was 100
        implied_risk = trade.qty * (trade.entry_price - stop_level)
        assert implied_risk <= risk_capital * 1.05  # within tolerance (slippage on entry)

    def test_open_position_liquidated_at_end(self):
        candles = flat_candles(90)
        result = Backtester(_cfg()).run(
            ScriptedStrategy(entry_indices=(85,), stop_pct=50), candles)
        assert result.trades[0].exit_reason == "end_of_data"


class TestNoLookahead:
    def test_strategy_never_sees_future_bars(self):
        candles = make_candles(200)
        strat = ScriptedStrategy(entry_indices=())
        Backtester(_cfg()).run(strat, candles)
        # at decision index i the last visible close_time must be candles[i].close_time
        # and the engine never exposes the bar used for the fill
        for length, last_seen in zip(strat.seen_history_lengths, strat.seen_last_close_time):
            assert last_seen == candles[length - 1].close_time
        assert max(strat.seen_history_lengths) <= len(candles) - 1

    def test_future_data_change_does_not_alter_past_trades(self):
        candles = make_candles(300, seed=11)
        strat_kwargs = dict(entry_indices=(80,), stop_pct=3, target_pct=3)
        r1 = Backtester(_cfg()).run(ScriptedStrategy(**strat_kwargs), candles)
        mutated = candles[:250] + [
            c.model_copy(update={"close": c.close * 2, "high": c.high * 2}) for c in candles[250:]
        ]
        r2 = Backtester(_cfg()).run(ScriptedStrategy(**strat_kwargs), mutated)
        t1, t2 = r1.trades[0], r2.trades[0]
        assert (t1.entry_time, t1.entry_price, t1.exit_time, t1.exit_price) == \
               (t2.entry_time, t2.entry_price, t2.exit_time, t2.exit_price)


class TestMetrics:
    def test_metrics_basic(self):
        candles = flat_candles(120)
        candles[70] = candles[70].model_copy(update={"high": 120.0, "open": 100.0})
        cfg = _cfg()
        result = Backtester(cfg).run(
            ScriptedStrategy(entry_indices=(66,), stop_pct=10, target_pct=5), candles)
        m = compute_metrics(result, initial_equity=cfg.initial_equity)
        assert m["n_trades"] == 1
        assert m["win_rate_pct"] == 100.0
        assert m["total_return_pct"] > 0
        assert m["fees_paid"] > 0
        assert "max_drawdown_pct" in m

    def test_losing_streak_and_profit_factor(self):
        candles = flat_candles(300)
        for idx in (70, 90, 110):
            candles[idx] = candles[idx].model_copy(update={"low": 90.0, "open": 100.0})
        cfg = _cfg()
        result = Backtester(cfg).run(
            ScriptedStrategy(entry_indices=(66, 86, 106), stop_pct=2), candles)
        m = compute_metrics(result, initial_equity=cfg.initial_equity)
        assert m["n_trades"] == 3
        assert m["win_rate_pct"] == 0.0
        assert m["longest_losing_streak"] == 3
        assert m["profit_factor"] == 0.0


class TestWalkForward:
    def test_windows_are_disjoint_with_purge(self):
        windows = make_windows(10_000, train_bars=4000, test_bars=1000, step_bars=1000,
                               purge_bars=200)
        assert windows
        for w in windows:
            assert w.test_start - w.train_end == 200
        for a, b in zip(windows, windows[1:]):
            assert b.train_start == a.train_start + 1000

    def test_baseline_strategies_run_end_to_end(self):
        from app.strategies.baselines import BASELINE_STRATEGIES

        candles = make_candles(600, drift=0.001, vol=0.01, seed=42)
        for cls in BASELINE_STRATEGIES.values():
            result = Backtester(_cfg(warmup_bars=210)).run(cls(), candles)
            m = compute_metrics(result, initial_equity=10_000)
            assert "n_trades" in m  # runs clean; profitability not asserted
