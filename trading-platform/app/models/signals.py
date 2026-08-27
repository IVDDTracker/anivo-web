"""Signal, decision-pipeline and evidence models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Direction, PipelineStage, Regime, SignalDecision, Venue


class EvidenceItem(BaseModel):
    name: str
    value: float | str | bool
    weight: float = 0.0
    supports: bool = True  # False → counter-evidence


class Signal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    strategy: str
    strategy_version: str
    direction: Direction
    timestamp: datetime
    timeframe: str = "1h"
    reference_price: float = Field(gt=0)
    confidence: float = Field(ge=0.0, le=100.0)  # 0-100
    expected_edge_bps: float = 0.0
    invalidation_level: float | None = None
    hypothetical_stop: float | None = None
    hypothetical_target: float | None = None
    expected_holding_period_hours: float | None = None
    market_regime: Regime = Regime.UNKNOWN
    evidence: list[EvidenceItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_quality: float = Field(default=1.0, ge=0.0, le=1.0)
    external_confirmation: float = Field(default=0.0, ge=-1.0, le=1.0)
    features_used: dict[str, float] = Field(default_factory=dict)


class FusionScore(BaseModel):
    """Meta-signal component scores (each 0-100, direction-adjusted)."""

    technical_score: float = 50.0
    momentum_score: float = 50.0
    microstructure_score: float = 50.0
    volume_score: float = 50.0
    volatility_score: float = 50.0
    market_regime_score: float = 50.0
    event_score: float = 50.0
    sentiment_score: float = 50.0
    cross_asset_score: float = 50.0
    liquidity_score: float = 50.0
    final_score: float = 0.0
    weights_version: str = "prior-v1"


class StageResult(BaseModel):
    stage: PipelineStage
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """Full audit trail of one signal through the pipeline."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str
    symbol: str
    strategy: str
    timestamp: datetime
    stages: list[StageResult] = Field(default_factory=list)
    decision: SignalDecision = SignalDecision.DO_NOTHING
    venue: Venue | None = None
    fusion: FusionScore | None = None
    explanation: str = ""

    def failed_stage(self) -> PipelineStage | None:
        for s in self.stages:
            if not s.passed:
                return s.stage
        return None
