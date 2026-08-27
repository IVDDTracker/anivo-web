"""Strategy registry: instances, lifecycle stages, persistence.

Lifecycle: EXPERIMENTAL → BACKTESTED → VALIDATED → PAPER → TESTNET, with
DEGRADED/DISABLED demotions. Promotion goes through PromotionEngine only.

Bootstrap default [documented]: the three baselines start at PAPER so a fresh
deployment paper-trades immediately; TESTNET always requires passing promotion
criteria — there is no bootstrap shortcut to TESTNET.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.logging import get_logger
from app.models.enums import StrategyStage
from app.storage.db import Database
from app.storage.tables import StrategyRow
from app.strategies.base import BaseStrategy
from app.strategies.baselines import BASELINE_STRATEGIES

log = get_logger(__name__)


@dataclass
class StrategyRecord:
    instance: BaseStrategy
    stage: StrategyStage = StrategyStage.EXPERIMENTAL
    enabled: bool = True
    scorecard: float = 0.0


@dataclass
class StrategyRegistry:
    clock: Clock
    db: Database | None = None
    records: dict[str, StrategyRecord] = field(default_factory=dict)

    def register(self, strategy: BaseStrategy,
                 stage: StrategyStage = StrategyStage.EXPERIMENTAL) -> None:
        self.records[strategy.name] = StrategyRecord(instance=strategy, stage=stage)

    def register_baselines(self, initial_stage: StrategyStage = StrategyStage.PAPER) -> None:
        for cls in BASELINE_STRATEGIES.values():
            self.register(cls(), stage=initial_stage)

    def active(self) -> list[StrategyRecord]:
        return [r for r in self.records.values()
                if r.enabled and r.stage in (StrategyStage.PAPER, StrategyStage.TESTNET)]

    def stages(self) -> dict[str, StrategyStage]:
        return {name: r.stage for name, r in self.records.items()}

    async def set_stage(self, name: str, stage: StrategyStage, reason: str = "") -> None:
        record = self.records.get(name)
        if record is None:
            return
        old = record.stage
        record.stage = stage
        log.warning("strategy %s stage %s → %s (%s)", name, old, stage, reason)
        await self.persist()

    async def persist(self) -> None:
        if self.db is None:
            return
        async with self.db.session() as s:
            for name, record in self.records.items():
                row = await s.get(StrategyRow, name)
                if row is None:
                    row = StrategyRow(name=name, updated_at=self.clock.now())
                    s.add(row)
                row.stage = record.stage.value
                row.enabled = record.enabled
                row.current_version = record.instance.version
                row.scorecard = record.scorecard
                row.updated_at = self.clock.now()

    async def restore(self) -> None:
        """Stages persisted in DB win over bootstrap defaults (restart safety)."""
        if self.db is None:
            return
        from sqlalchemy import select

        async with self.db.session() as s:
            rows = (await s.scalars(select(StrategyRow))).all()
        for row in rows:
            record = self.records.get(row.name)
            if record is not None:
                record.stage = StrategyStage(row.stage)
                record.enabled = row.enabled
                record.scorecard = row.scorecard
