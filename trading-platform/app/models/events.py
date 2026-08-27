"""Event domain models: raw collector events and normalized external events."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EventCategory, SourceType


class RawEvent(BaseModel):
    """Envelope every collector emits. Persisted before processing → replayable."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    source_type: SourceType
    timestamp_received: datetime
    timestamp_event: datetime
    symbol: str | None = None
    kind: str = ""  # e.g. kline_1m, trade, bookTicker, tg_message, gh_release, rss_item
    raw_payload: dict = Field(default_factory=dict)
    normalized_payload: dict | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_reliability: float = Field(default=1.0, ge=0.0, le=1.0)
    event_hash: str


class ExternalEvent(BaseModel):
    """Normalized external intelligence event (news, telegram, github, ...)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    assets: list[str] = Field(default_factory=list)
    category: EventCategory = EventCategory.OTHER
    headline: str = Field(max_length=500)
    body_excerpt: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=1000)
    timestamp: datetime
    source: str
    source_type: SourceType
    reliability: float = Field(ge=0.0, le=1.0)
    sentiment: float = Field(default=0.0, ge=-1.0, le=1.0)
    magnitude: float = Field(default=0.0, ge=0.0, le=1.0)
    novelty: float = Field(default=1.0, ge=0.0, le=1.0)
    confirmation_count: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    decay_half_life_hours: float = Field(default=12.0, gt=0.0)
    event_hash: str
    cluster_key: str

    def decayed_weight(self, now: datetime) -> float:
        """confidence * magnitude, exponentially decayed by age."""
        age_h = max(0.0, (now - self.timestamp).total_seconds() / 3600.0)
        decay = 0.5 ** (age_h / self.decay_half_life_hours)
        return self.confidence * max(self.magnitude, 0.05) * decay
