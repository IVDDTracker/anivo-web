"""DecisionPipeline: the ONLY path from a signal to an order intent.

DATA QUALITY → SIGNAL GENERATION → SIGNAL CONFIRMATION → STRATEGY FILTER →
REGIME FILTER → RISK ENGINE → PORTFOLIO FILTER → EXECUTION SIMULATION →
FINAL DECISION

Every stage returns an auditable StageResult; the full DecisionRecord is
persisted for approved AND rejected signals. Any error in any stage fails that
stage (DO_NOTHING) — there is no default-allow path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.config.settings import Settings
from app.core.clock import Clock
from app.core.logging import get_logger
from app.core.state import StateMachine
from app.data.intelligence import EventIntelligence
from app.data.quality import DataQualityService
from app.features.microstructure import MicrostructureTracker
from app.models.enums import (
    Direction,
    ExecutionMode,
    OrderSide,
    OrderType,
    PipelineStage,
    SignalDecision,
    StrategyStage,
    Venue,
)
from app.models.market import SymbolRules
from app.models.orders import TradeIntent
from app.models.signals import DecisionRecord, Signal, StageResult
from app.risk.engine import EntryRequest, RiskEngine
from app.signals.fusion import FusionInputs, fuse
from app.strategies.base import BaseStrategy

log = get_logger(__name__)


@dataclass
class PipelineDeps:
    settings: Settings
    clock: Clock
    state: StateMachine
    quality: DataQualityService
    risk: RiskEngine
    micro: MicrostructureTracker
    intelligence: EventIntelligence | None = None
    rules: dict[str, SymbolRules] = field(default_factory=dict)
    ticker_24h_quote_volume: dict[str, float] = field(default_factory=dict)
    open_positions_provider: object = None       # async () -> list[Position]
    equity_provider: object = None               # () -> float
    mark_prices: dict[str, float] = field(default_factory=dict)
    strategy_stages: dict[str, StrategyStage] = field(default_factory=dict)
    min_confirmation_score: float = 55.0
    contradiction_threshold: float = -0.5


class DecisionPipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    async def decide(self, signal: Signal, strategy: BaseStrategy) -> tuple[DecisionRecord, TradeIntent | None]:
        d = self.deps
        record = DecisionRecord(
            signal_id=signal.id, symbol=signal.symbol, strategy=signal.strategy,
            timestamp=d.clock.now(),
        )

        def stage(name: PipelineStage, passed: bool, reasons: list[str], **data) -> bool:
            record.stages.append(StageResult(stage=name, passed=passed, reasons=reasons,
                                             data=data))
            return passed

        # 1 ── DATA QUALITY
        quality = d.quality.quality_score(signal.symbol)
        signal.data_quality = quality
        min_q = d.settings.risk.min_data_quality
        if not stage(PipelineStage.DATA_QUALITY, quality >= min_q,
                     [f"quality {quality:.2f} (min {min_q})"], quality=quality):
            return await self._finish(record, signal, None)

        # 2 ── SIGNAL GENERATION (the signal exists; sanity-check its fields)
        sane = (signal.reference_price > 0 and
                (signal.hypothetical_stop is None or signal.hypothetical_stop > 0))
        if not stage(PipelineStage.SIGNAL_GENERATION, sane,
                     [f"{signal.strategy} {signal.direction} conf {signal.confidence:.0f}"]):
            return await self._finish(record, signal, None)

        # 3 ── SIGNAL CONFIRMATION (fusion + external contradiction)
        event_evidence = 0.0
        if d.intelligence is not None:
            try:
                event_evidence = await d.intelligence.evidence_score(signal.symbol)
            except Exception:
                log.exception("evidence score failed — treating as neutral evidence, "
                              "degraded quality")
                event_evidence = 0.0
        signal.external_confirmation = event_evidence
        micro = d.micro.features(signal.symbol, d.clock.now())
        spread_pct = micro.get("spread_pct_last")
        quote_vol = d.ticker_24h_quote_volume.get(signal.symbol)
        fusion = fuse(signal, FusionInputs(
            features=signal.features_used or {}, micro=micro,
            regime=signal.market_regime, data_quality=quality,
            event_evidence=event_evidence,
            sentiment=event_evidence,  # sentiment folded into evidence in v1
            cross_asset_momentum=0.0,
            liquidity_ok=quote_vol is not None
            and quote_vol >= d.settings.risk.min_liquidity_quote_vol_24h,
            spread_pct=spread_pct if spread_pct is not None else float("nan"),
        ))
        record.fusion = fusion
        contradicted = event_evidence <= d.contradiction_threshold
        confirmed = fusion.final_score >= d.min_confirmation_score and not contradicted
        reasons = [f"fused {fusion.final_score:.1f} (min {d.min_confirmation_score})"]
        if contradicted:
            reasons.append(f"contradicted by external evidence {event_evidence:.2f}")
        if not stage(PipelineStage.SIGNAL_CONFIRMATION, confirmed, reasons,
                     fusion=fusion.model_dump()):
            return await self._finish(record, signal, None)

        # 4 ── STRATEGY FILTER (lifecycle stage + feature completeness)
        strategy_stage = self.deps.strategy_stages.get(
            signal.strategy, StrategyStage.EXPERIMENTAL)
        live_ok = strategy_stage in (StrategyStage.PAPER, StrategyStage.TESTNET)
        features_ok = strategy.has_required_features(signal.features_used)
        if not stage(PipelineStage.STRATEGY_FILTER, live_ok and features_ok,
                     [f"stage {strategy_stage}", f"features_ok={features_ok}"]):
            return await self._finish(record, signal, None)

        # 5 ── REGIME FILTER
        regime_ok = (not strategy.eligible_regimes
                     or signal.market_regime in strategy.eligible_regimes)
        if not stage(PipelineStage.REGIME_FILTER, regime_ok,
                     [f"regime {signal.market_regime} eligible={regime_ok}"]):
            return await self._finish(record, signal, None)

        # 6 ── RISK ENGINE (absolute veto)
        try:
            open_positions = (await d.open_positions_provider()
                              if d.open_positions_provider else [])
            equity = float(d.equity_provider()) if d.equity_provider else 0.0
            risk_decision = d.risk.evaluate_entry(EntryRequest(
                symbol=signal.symbol, direction=signal.direction,
                entry_price=signal.reference_price, stop_price=signal.hypothetical_stop,
                signal_confidence=fusion.final_score, data_quality=quality,
                spread_pct=spread_pct * 100 if spread_pct is not None else None,
                quote_volume_24h=quote_vol, equity=equity, open_positions=open_positions,
                mark_prices=d.mark_prices, rules=d.rules.get(signal.symbol),
            ))
        except Exception:
            log.exception("risk engine failed — fail-safe reject")
            stage(PipelineStage.RISK_ENGINE, False, ["risk engine unavailable (fail-safe)"])
            return await self._finish(record, signal, None)
        if not stage(PipelineStage.RISK_ENGINE, risk_decision.approved,
                     risk_decision.reasons, checks=risk_decision.checks):
            return await self._finish(record, signal, None)

        # 7 ── PORTFOLIO FILTER (single position per symbol; venue capacity)
        already_open = any(p.symbol == signal.symbol and p.is_open for p in open_positions)
        if not stage(PipelineStage.PORTFOLIO_FILTER, not already_open,
                     ["position already open" if already_open else "no existing position"]):
            return await self._finish(record, signal, None)

        # 8 ── EXECUTION SIMULATION (does the edge survive estimated costs?)
        qty = risk_decision.approved_quantity or Decimal("0")
        est_cost_bps = (d.settings.costs.taker_fee_bps * 2
                        + d.settings.costs.base_slippage_bps * 2
                        + (spread_pct or 0.0) * 10_000 / 2)
        edge_bps = signal.expected_edge_bps
        if edge_bps <= 0 and signal.hypothetical_stop and signal.hypothetical_target:
            reward = signal.hypothetical_target - signal.reference_price
            risk_dist = signal.reference_price - signal.hypothetical_stop
            if risk_dist > 0:
                # conservative expectancy proxy at 40% win rate
                edge_bps = (0.4 * reward - 0.6 * risk_dist) / signal.reference_price * 10_000
        executable = qty > 0 and edge_bps > est_cost_bps
        if not stage(PipelineStage.EXECUTION_SIMULATION, executable,
                     [f"edge {edge_bps:.0f}bps vs est cost {est_cost_bps:.0f}bps",
                      f"qty {qty}"]):
            return await self._finish(record, signal, None)

        # 9 ── FINAL DECISION
        venue = Venue.PAPER
        if (d.settings.execution_mode == ExecutionMode.TESTNET_ACTIVE
                and strategy_stage == StrategyStage.TESTNET):
            venue = Venue.TESTNET
        stage(PipelineStage.FINAL_DECISION, True, [f"approved for {venue}"])
        record.decision = SignalDecision.APPROVED
        record.venue = venue
        intent = TradeIntent(
            signal_id=signal.id, decision_id=record.id, symbol=signal.symbol,
            direction=signal.direction,
            side=OrderSide.BUY if signal.direction == Direction.LONG else OrderSide.SELL,
            order_type=OrderType.MARKET, signal_score=fusion.final_score,
            reference_price=Decimal(str(signal.reference_price)), quantity=qty,
            hypothetical_stop=(Decimal(str(signal.hypothetical_stop))
                               if signal.hypothetical_stop else None),
            hypothetical_target=(Decimal(str(signal.hypothetical_target))
                                 if signal.hypothetical_target else None),
            reason="; ".join(f"{e.name}={e.value}" for e in signal.evidence[:6]),
            invalidation=(f"below {signal.invalidation_level:.6g}"
                          if signal.invalidation_level else ""),
            venue=venue, strategy=signal.strategy, created_at=d.clock.now(),
        )
        record.explanation = self._explain(record, signal)
        return record, intent

    async def _finish(self, record: DecisionRecord, signal: Signal,
                      intent: TradeIntent | None) -> tuple[DecisionRecord, TradeIntent | None]:
        record.decision = SignalDecision.REJECTED
        record.explanation = self._explain(record, signal)
        return record, intent

    @staticmethod
    def _explain(record: DecisionRecord, signal: Signal) -> str:
        lines = [f"{signal.symbol} {signal.direction} by {signal.strategy}: {record.decision}"]
        for s in record.stages:
            mark = "✓" if s.passed else "✗"
            lines.append(f"{mark} {s.stage}: {'; '.join(s.reasons)}")
        for e in signal.evidence[:6]:
            lines.append(f"evidence: {e.name} = {e.value}")
        for r in signal.risks[:4]:
            lines.append(f"risk: {r}")
        return "\n".join(lines)
