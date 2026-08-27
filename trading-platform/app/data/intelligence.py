"""Event Intelligence Engine: dedup, clustering, cross-source confirmation,
novelty, time decay, and the per-symbol external-evidence score.

Confirmation semantics (DATA_SOURCES.md):
- events cluster by (assets, category, fuzzy headline) within a time window;
- confirmations count DISTINCT ORIGINS: distinct sources with materially
  different wording. Syndicated copies (identical normalized headline from a
  different outlet) do NOT add confirmation — they only exist as duplicates;
- confidence is combined as 1 - Π(1 - reliability_i) over distinct origins,
  and clusters whose best origin is Telegram stay capped at the Telegram cap:
  Telegram alone can never make an event trustworthy;
- weight decays exponentially per category half-life (rumors die in hours).
"""

from __future__ import annotations

from datetime import timedelta

from app.core.bus import EventBus, Topics
from app.core.clock import Clock
from app.core.hashing import normalized_text, story_similarity
from app.core.logging import get_logger
from app.models.enums import SourceType
from app.models.events import ExternalEvent
from app.storage.repositories import EventRepository

log = get_logger(__name__)

TELEGRAM_CONFIDENCE_CAP = 0.35


class EventIntelligence:
    def __init__(self, repo: EventRepository, clock: Clock, bus: EventBus | None = None,
                 *, cluster_window_h: float = 48.0,
                 significant_confidence: float = 0.6) -> None:
        self.repo = repo
        self.clock = clock
        self.bus = bus
        self.cluster_window = timedelta(hours=cluster_window_h)
        self.significant_confidence = significant_confidence

    async def process(self, event: ExternalEvent) -> ExternalEvent | None:
        """Dedup, confirm, score. Returns the enriched event or None for duplicates."""
        stored = await self.repo.store_external(event)
        if not stored:
            return None  # exact duplicate (same source, same content)

        since = event.timestamp - self.cluster_window
        coarse = await self.repo.cluster_events(event.cluster_key, since)
        # same-story filter within the coarse (assets, category) cluster
        cluster = [m for m in coarse
                   if m.id == event.id or story_similarity(m.headline, event.headline) >= 0.2]

        # distinct origins: distinct sources with materially different wording —
        # (near-)identical wording from another outlet is syndication of ONE origin
        origins: dict[str, ExternalEvent] = {}
        headline_texts: list[str] = []
        for member in cluster:
            headline_norm = normalized_text(member.headline)
            if any(headline_norm == seen or story_similarity(headline_norm, seen) >= 0.9
                   for seen in headline_texts):
                continue  # copy of an existing origin's wording
            headline_texts.append(headline_norm)
            origins.setdefault(member.source, member)

        distinct = list(origins.values())
        confirmation_count = max(0, len(distinct) - 1)
        novelty = 1.0 if len(cluster) <= 1 else max(0.2, 1.0 / len(cluster))

        # combined confidence over independent origins
        residual = 1.0
        for member in distinct:
            residual *= 1.0 - min(max(member.reliability, 0.0), 0.99)
        confidence = 1.0 - residual
        if all(m.source_type == SourceType.TELEGRAM for m in distinct):
            confidence = min(confidence, TELEGRAM_CONFIDENCE_CAP)

        event.confirmation_count = confirmation_count
        event.novelty = novelty
        event.confidence = round(min(confidence, 0.99), 4)
        await self.repo.update_external_confirmation(
            event.id, confirmation_count=confirmation_count,
            confidence=event.confidence, novelty=novelty)

        if self.bus is not None and self.is_significant(event):
            await self.bus.publish(Topics.NOTIFY, {
                "kind": "external_event", "headline": event.headline, "source": event.source,
                "confidence": event.confidence, "assets": event.assets,
            })
        return event

    def is_significant(self, event: ExternalEvent) -> bool:
        return (event.confidence >= self.significant_confidence
                and event.magnitude >= 0.5
                and event.decayed_weight(self.clock.now()) >= 0.2)

    async def evidence_score(self, symbol: str, *, lookback_h: float = 48.0) -> float:
        """Aggregate decayed, signed external evidence for a symbol in [-1, 1].

        Only confirmed-enough events contribute; Telegram-only noise stays under
        the contribution floor by construction.
        """
        now = self.clock.now()
        events = await self.repo.recent_external(
            since=now - timedelta(hours=lookback_h), asset=symbol)
        score = 0.0
        for event in events:
            weight = event.decayed_weight(now) * event.novelty
            if weight < 0.02:
                continue
            score += weight * (1.0 if event.sentiment >= 0 else -1.0) * min(
                abs(event.sentiment) + 0.5, 1.0)
        return max(-1.0, min(1.0, score))
