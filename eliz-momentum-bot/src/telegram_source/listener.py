"""Telegram channel ingestion via YOUR OWN user account (Telethon / MTProto).

Cost: free. Latency: typically 1-3 seconds from post to delivery — more than
enough for the SHORT_ONLY strategy, which trades the reversal, not the spike.

Ground rules:
- Only channels/groups you explicitly configured AND joined with your own
  account are read (`TG_SOURCE_CHANNELS`). No scraping tricks, no mass joins —
  a handful of channels keeps the account well within Telegram's normal-use
  behavior.
- Messages are DATA, never instructions: they flow through the same
  parser/classifier gates as tweets, and in SHORT_ONLY mode a message alone
  can never open a position — a real pump must print on the exchange first,
  then a confirmed reversal.
- OBSERVE, don't participate: this bot must never be used to join/coordinate a
  pump. It trades against the aftermath using public market data.

Messages are converted into the same `TweetEvent` envelope the X listener
emits (`tg:<chat_id>:<msg_id>` ids), so the entire downstream pipeline —
classifier, session, storage, event study — is shared, source-agnostic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from src.core.clock import utcnow
from src.core.domain import TweetEvent, TweetKind
from src.core.logger import get_logger

log = get_logger(__name__)

OnMessage = Callable[[TweetEvent], Awaitable[None]]


def message_to_event(*, chat_id: int | str, message_id: int, text: str,
                     date: datetime, channel_name: str = "",
                     received_at: datetime | None = None) -> TweetEvent | None:
    """Pure converter (unit-tested without Telethon). None → not usable."""
    text = (text or "").strip()
    if len(text) < 3:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return TweetEvent(
        tweet_id=f"tg:{chat_id}:{message_id}",
        author_id=str(channel_name or chat_id),
        text=text,
        kind=TweetKind.ORIGINAL,
        created_at=date.astimezone(UTC),
        received_at=received_at or utcnow(),
        raw={"source": "telegram", "chat_id": str(chat_id), "message_id": message_id,
             "channel": channel_name},
    )


class TelegramSourceListener:
    """Live listener. Requires TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION
    (create the session string once with `python -m src.telegram_source.login`)."""

    def __init__(self, *, api_id: int, api_hash: str, session: str,
                 channels: list[str], on_message: OnMessage) -> None:
        if not (api_id and api_hash and session):
            raise ValueError("TELEGRAM_API_ID/TELEGRAM_API_HASH/TELEGRAM_SESSION required "
                             "— run `python -m src.telegram_source.login` first")
        if not channels:
            raise ValueError("TG_SOURCE_CHANNELS is empty — nothing to listen to")
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.channels = channels
        self.on_message = on_message
        self._seen: set[str] = set()
        self.last_event_at: datetime | None = None
        self.healthy = False

    async def _emit(self, event: TweetEvent | None) -> None:
        if event is None or event.tweet_id in self._seen:
            return
        self._seen.add(event.tweet_id)
        if len(self._seen) > 20_000:
            self._seen = set(list(self._seen)[-4_000:])
        self.last_event_at = event.received_at
        log.info("telegram message %s from %s (latency %.1fs)",
                 event.tweet_id, event.author_id, event.latency_ms / 1000)
        await self.on_message(event)

    async def run(self) -> None:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(self.session), self.api_id, self.api_hash)
        await client.start()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("telegram session not authorized — re-run "
                                   "`python -m src.telegram_source.login`")
            entities = []
            for channel in self.channels:
                try:
                    entities.append(await client.get_entity(channel))
                except Exception as exc:
                    log.warning("cannot resolve channel %s (%s) — join it with this "
                                "account first", channel, str(exc)[:120])
            if not entities:
                raise RuntimeError("no configured telegram channel could be resolved")

            @client.on(events.NewMessage(chats=entities))
            async def handler(update) -> None:  # noqa: ANN001
                message = update.message
                channel = getattr(update.chat, "username", "") or \
                    getattr(update.chat, "title", "")
                await self._emit(message_to_event(
                    chat_id=update.chat_id, message_id=message.id,
                    text=message.message or "", date=message.date,
                    channel_name=str(channel)))

            self.healthy = True
            log.info("telegram source connected: %d channels", len(entities))
            await client.run_until_disconnected()
        finally:
            self.healthy = False
            await client.disconnect()


async def fetch_channel_history(*, api_id: int, api_hash: str, session: str,
                                channel: str, limit: int = 2000) -> list[TweetEvent]:
    """Historical messages for the event study (same envelope as live)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.start()
    try:
        out: list[TweetEvent] = []
        async for message in client.iter_messages(channel, limit=limit):
            event = message_to_event(
                chat_id=message.chat_id, message_id=message.id,
                text=message.message or "", date=message.date,
                channel_name=channel, received_at=message.date)
            if event is not None:
                out.append(event)
        out.reverse()  # chronological
        return out
    finally:
        await client.disconnect()


async def _sleep_forever() -> None:  # pragma: no cover - helper for manual runs
    await asyncio.Event().wait()
