"""Promotion engine (no stage skipping), scorecard, degradation detection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.config.settings import PromotionConfig
from app.models.enums import StrategyStage, Venue
from app.models.orders import Position
from app.monitoring.degradation import DegradationConfig, DegradationDetector
from app.strategies.promotion import PromotionEngine, PromotionEvidence
from app.strategies.registry import StrategyRegistry
from app.strategies.scorecard import ScorecardInputs, compute_scorecard

ENGINE = PromotionEngine(PromotionConfig())

GOOD = PromotionEvidence(
    backtest_trades=150, backtest_profit_factor=1.5, backtest_max_dd_pct=12.0,
    oos_trades=40, oos_profit_factor=1.3, oos_expectancy=2.5, parameter_stable=True,
    min_perturbed_pf=1.1, largest_winner_share=0.2, scorecard=70.0,
    paper_days=20, paper_trades=15, paper_expectancy=1.0,
)


class TestPromotion:
    def test_full_ladder_with_good_evidence(self):
        stage = StrategyStage.EXPERIMENTAL
        for expected in (StrategyStage.BACKTESTED, StrategyStage.VALIDATED,
                         StrategyStage.PAPER, StrategyStage.TESTNET):
            result = ENGINE.evaluate(stage, GOOD)
            assert result.promoted, result.reasons
            assert result.to_stage == expected  # exactly one step at a time
            stage = result.to_stage

    def test_cannot_skip_stages(self):
        result = ENGINE.evaluate(StrategyStage.EXPERIMENTAL, GOOD)
        assert result.to_stage == StrategyStage.BACKTESTED  # never straight to TESTNET

    def test_too_few_trades_blocks(self):
        import dataclasses

        ev = dataclasses.replace(GOOD, backtest_trades=20)
        result = ENGINE.evaluate(StrategyStage.EXPERIMENTAL, ev)
        assert not result.promoted

    def test_unstable_parameters_block_validation(self):
        import dataclasses

        ev = dataclasses.replace(GOOD, parameter_stable=False, min_perturbed_pf=0.6)
        result = ENGINE.evaluate(StrategyStage.BACKTESTED, ev)
        assert not result.promoted
        assert any("perturbed" in r for r in result.reasons)

    def test_single_winner_dependence_blocks(self):
        import dataclasses

        ev = dataclasses.replace(GOOD, largest_winner_share=0.7)
        assert not ENGINE.evaluate(StrategyStage.BACKTESTED, ev).promoted

    def test_testnet_needs_paper_history(self):
        import dataclasses

        ev = dataclasses.replace(GOOD, paper_days=3)
        assert not ENGINE.evaluate(StrategyStage.PAPER, ev).promoted

    def test_every_decision_has_reasons(self):
        result = ENGINE.evaluate(StrategyStage.EXPERIMENTAL, GOOD)
        assert result.reasons and all(r.startswith(("✓", "✗")) for r in result.reasons)


class TestScorecard:
    def test_strong_strategy_scores_high(self):
        score = compute_scorecard(ScorecardInputs(
            oos_profit_factor=1.8, oos_trades=120, profitable_windows=5, total_windows=6,
            max_drawdown_pct=8.0, sharpe=1.6, parameter_stable=True, min_perturbed_pf=1.3,
            profitable_assets=3, tested_assets=3, fee_sensitivity_ratio=0.9))
        assert score["total"] >= 75

    def test_overfit_strategy_scores_low(self):
        score = compute_scorecard(ScorecardInputs(
            oos_profit_factor=0.8, oos_trades=12, profitable_windows=1, total_windows=6,
            max_drawdown_pct=30.0, sharpe=-0.5, parameter_stable=False, min_perturbed_pf=0.4,
            profitable_assets=1, tested_assets=3, fee_sensitivity_ratio=0.2,
            recent_degradation=True))
        assert score["total"] < 30

    def test_parts_sum_to_total(self):
        score = compute_scorecard(ScorecardInputs())
        assert abs(sum(score["parts"].values()) - score["total"]) < 0.1


def _pos(strategy: str, pnl: str) -> Position:
    return Position(venue=Venue.PAPER, symbol="BTCUSDT", qty=Decimal("0"),
                    avg_entry_price=Decimal("100"), strategy=strategy,
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                    closed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    realized_pnl=Decimal(pnl))


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def strategy_degraded(self, name, detail):
        self.alerts.append((name, detail))


class TestDegradation:
    async def test_losing_streak_degrades_but_keeps_observing(self, sim_clock):
        registry = StrategyRegistry(clock=sim_clock)
        registry.register_baselines(initial_stage=StrategyStage.TESTNET)
        notifier = FakeNotifier()
        detector = DegradationDetector(
            registry=registry, clock=sim_clock, notifier=notifier,
            cfg=DegradationConfig(max_losing_streak=3, min_trades_to_judge=99))
        for _ in range(3):
            await detector.on_position_closed(_pos("trend_momentum", "-10"))
        assert registry.records["trend_momentum"].stage == StrategyStage.DEGRADED
        assert notifier.alerts and notifier.alerts[0][0] == "trend_momentum"
        # degraded ≠ disabled: it is out of active (testnet/paper trading) rotation
        assert registry.records["trend_momentum"].enabled
        assert all(r.instance.name != "trend_momentum" for r in registry.active())

    async def test_negative_expectancy_degrades(self, sim_clock):
        registry = StrategyRegistry(clock=sim_clock)
        registry.register_baselines(initial_stage=StrategyStage.PAPER)
        detector = DegradationDetector(
            registry=registry, clock=sim_clock,
            cfg=DegradationConfig(min_trades_to_judge=5, max_losing_streak=99))
        pnls = ["-5", "3", "-6", "2", "-7", "-4"]
        for p in pnls:
            await detector.on_position_closed(_pos("volume_breakout", p))
        assert registry.records["volume_breakout"].stage == StrategyStage.DEGRADED

    async def test_profitable_strategy_untouched(self, sim_clock):
        registry = StrategyRegistry(clock=sim_clock)
        registry.register_baselines(initial_stage=StrategyStage.PAPER)
        detector = DegradationDetector(registry=registry, clock=sim_clock,
                                       cfg=DegradationConfig(min_trades_to_judge=5))
        for p in ("5", "-2", "6", "3", "-1", "4"):
            await detector.on_position_closed(_pos("trend_momentum", p))
        assert registry.records["trend_momentum"].stage == StrategyStage.PAPER
