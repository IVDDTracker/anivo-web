"""Telegram message ingestion → LOW-TRUST ExternalEvents.

Hard rules:
- reliability is capped (default 0.35) regardless of configuration;
- only messages from chats explicitly configured AND joined by the bot arrive here
  (routing enforced by TelegramBot);
- resulting events can only influence trading through the event-intelligence
  score after cross-source confirmation — never directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config.sources import TelegramSourceCfg
from app.core.bus import EventBus, Topics
from app.core.logging import get_logger
from app.models.enums import SourceType
from app.models.events import ExternalEvent

log = get_logger(__name__)


class TelegramIngest:
    def __init__(self, sources: list[TelegramSourceCfg], bus: EventBus,
                 *, reliability_cap: float = 0.35) -> None:
        self.bus = bus
        self.reliability_cap = reliability_cap
        self.sources_by_chat: dict[str, TelegramSourceCfg] = {
            str(s.identifier): s for s in sources if s.enabled
        }

    @property
    def chat_ids(self) -> set[str]:
        return set(self.sources_by_chat)

    async def handle_message(self, message: dict) -> ExternalEvent | None:
        from app.data.normalization.events import build_external_event

        chat_id = str((message.get("chat") or {}).get("id", ""))
        cfg = self.sources_by_chat.get(chat_id)
        if cfg is None:
            return None
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text or len(text) < 8:
            return None
        if cfg.keywords and not any(k.lower() in text.lower() for k in cfg.keywords):
            return None
        ts_raw = message.get("date")
        timestamp = (datetime.fromtimestamp(int(ts_raw), tz=UTC)
                     if isinstance(ts_raw, (int, float)) else datetime.now(UTC))
        event = build_external_event(
            text=text,
            source=f"telegram:{cfg.name}",
            source_type=SourceType.TELEGRAM,
            reliability=min(cfg.reliability_score, self.reliability_cap),
            timestamp=timestamp,
            restrict_assets=cfg.symbols or None,
        )
        await self.bus.publish(Topics.EXTERNAL_EVENT, event)
        return event
