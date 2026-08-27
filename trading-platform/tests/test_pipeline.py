"""DecisionPipeline: the nine-gate chain, fail-safe behavior, venue routing."""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.core.state import StateMachine
from app.data.quality import DataQualityService
from app.features.microstructure import MicrostructureTracker
from app.models.enums import (
    Direction,
    ExecutionMode,
    PipelineStage,
    Regime,
    SignalDecision,
    StrategyStage,
    Venue,
)
from app.models.market import BookTicker
from app.models.signals import Signal
from app.pipeline import DecisionPipeline, PipelineDeps
from app.risk.engine import RiskEngine
from app.strategies.base import BaseStrategy


class StubStrategy(BaseStrategy):
    name = "stub"
    version = "1.0"
    eligible_regimes = frozenset({Regime.STRONG_UPTREND, Regime.WEAK_UPTREND})

    def generate_signal(self, ctx):
        return None


def make_signal(**overrides) -> Signal:
    from datetime import UTC, datetime

    defaults = dict(
        symbol="BTCUSDT", strategy="stub", strategy_version="1.0",
        direction=Direction.LONG, timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        reference_price=100.0, confidence=75.0, hypothetical_stop=98.0,
        hypothetical_target=110.0, market_regime=Regime.STRONG_UPTREND,
        features_used={"ema_structure_bull": 1.0, "macd_hist": 1.0, "close": 100.0,
                       "bb_pct_b": 0.8, "momentum_20": 0.05, "volume_zscore": 2.0,
                       "vol_percentile": 0.5},
    )
    defaults.update(overrides)
    return Signal(**defaults)


@pytest.fixture
def deps(sim_clock):
    settings = Settings(_env_file=None)
    state = StateMachine(clock=sim_clock, execution_mode=settings.execution_mode)
    state.mark_started()
    quality = DataQualityService(clock=sim_clock, state=state)
    quality.on_price("BTCUSDT", 100.0, sim_clock.now())
    micro = MicrostructureTracker()
    micro.on_book_ticker(BookTicker(symbol="BTCUSDT", bid_price=99.99, bid_qty=50,
                                    ask_price=100.01, ask_qty=50, timestamp=sim_clock.now()))
    risk = RiskEngine(cfg=settings.risk, clock=sim_clock, state=state)
    risk.update_equity(10_000.0)

    async def open_positions():
        return []

    return PipelineDeps(
        settings=settings, clock=sim_clock, state=state, quality=quality, risk=risk,
        micro=micro, intelligence=None,
        ticker_24h_quote_volume={"BTCUSDT": 100_000_000.0},
        open_positions_provider=open_positions, equity_provider=lambda: 10_000.0,
        strategy_stages={"stub": StrategyStage.PAPER},
    )


async def test_happy_path_approved_with_paper_intent(deps):
    pipeline = DecisionPipeline(deps)
    record, intent = await pipeline.decide(make_signal(), StubStrategy())
    assert record.decision == SignalDecision.APPROVED, record.explanation
    assert intent is not None and intent.venue == Venue.PAPER
    assert intent.quantity > 0
    assert len(record.stages) == 9
    assert all(s.passed for s in record.stages)
    assert record.explanation  # every decision is explainable


async def test_stale_data_rejects_at_quality_gate(deps, sim_clock):
    from datetime import timedelta

    sim_clock.advance_to(sim_clock.now() + timedelta(hours=1))  # price now stale
    record, intent = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert record.decision == SignalDecision.REJECTED
    assert record.failed_stage() == PipelineStage.DATA_QUALITY
    assert intent is None


async def test_hostile_regime_zeroes_fusion(deps):
    signal = make_signal(market_regime=Regime.PANIC)
    record, intent = await DecisionPipeline(deps).decide(signal, StubStrategy())
    assert record.decision == SignalDecision.REJECTED
    assert record.failed_stage() == PipelineStage.SIGNAL_CONFIRMATION
    assert record.fusion.final_score == 0.0


async def test_experimental_strategy_cannot_trade(deps):
    deps.strategy_stages["stub"] = StrategyStage.EXPERIMENTAL
    record, intent = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert record.failed_stage() == PipelineStage.STRATEGY_FILTER
    assert intent is None


async def test_regime_filter_blocks_ineligible(deps):
    # RANGE fuses fine for longs but stub only accepts uptrends
    signal = make_signal(market_regime=Regime.RANGE)
    record, _ = await DecisionPipeline(deps).decide(signal, StubStrategy())
    assert record.failed_stage() in (PipelineStage.REGIME_FILTER,
                                     PipelineStage.SIGNAL_CONFIRMATION)


async def test_risk_engine_failure_fails_safe(deps):
    class BrokenRisk:
        def evaluate_entry(self, req):
            raise RuntimeError("db down")

    deps.risk = BrokenRisk()
    record, intent = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert record.decision == SignalDecision.REJECTED
    assert record.failed_stage() == PipelineStage.RISK_ENGINE
    assert intent is None


async def test_open_position_blocks_portfolio_stage(deps):
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.models.orders import Position

    async def with_open():
        return [Position(venue=Venue.PAPER, symbol="BTCUSDT", qty=Decimal("1"),
                         avg_entry_price=Decimal("100"),
                         opened_at=datetime(2026, 1, 1, tzinfo=UTC))]

    deps.open_positions_provider = with_open
    record, _ = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    # risk engine's asset-exposure cap or the portfolio single-position gate must stop it
    assert record.decision == SignalDecision.REJECTED
    assert record.failed_stage() in (PipelineStage.RISK_ENGINE, PipelineStage.PORTFOLIO_FILTER)


async def test_edge_must_survive_costs(deps):
    signal = make_signal(hypothetical_stop=99.9, hypothetical_target=100.05)  # ~no edge
    record, _ = await DecisionPipeline(deps).decide(signal, StubStrategy())
    assert record.failed_stage() == PipelineStage.EXECUTION_SIMULATION


async def test_testnet_routing_requires_stage_and_mode(deps):
    deps.settings = deps.settings.model_copy(
        update={"execution_mode": ExecutionMode.TESTNET_ACTIVE})
    deps.strategy_stages["stub"] = StrategyStage.TESTNET
    record, intent = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert record.decision == SignalDecision.APPROVED
    assert intent.venue == Venue.TESTNET


async def test_paper_mode_never_routes_testnet(deps):
    deps.strategy_stages["stub"] = StrategyStage.TESTNET
    record, intent = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert intent.venue == Venue.PAPER  # PAPER_ONLY mode wins


async def test_rejected_decisions_carry_stage_trail(deps):
    deps.strategy_stages["stub"] = StrategyStage.DISABLED
    record, _ = await DecisionPipeline(deps).decide(make_signal(), StubStrategy())
    assert [s.stage for s in record.stages][:4] == [
        PipelineStage.DATA_QUALITY, PipelineStage.SIGNAL_GENERATION,
        PipelineStage.SIGNAL_CONFIRMATION, PipelineStage.STRATEGY_FILTER]
    assert "✗" in record.explanation
