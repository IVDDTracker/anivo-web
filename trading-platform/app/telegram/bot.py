"""Telegram control bot + ingestion router.

Security model (SECURITY.md):
- CONTROL commands are honored ONLY from the configured admin chat id;
- messages from configured ingestion chats are parsed as LOW-TRUST data events;
- anything else is ignored entirely.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.data.collectors.base import BaseCollector
from app.models.enums import SourceType
from app.telegram.client import TelegramClient

log = get_logger(__name__)

CommandHandler = Callable[[str], Awaitable[str]]


@dataclass
class CommandRouter:
    """Maps /command → async handler returning the reply text."""

    handlers: dict[str, CommandHandler] = field(default_factory=dict)

    def register(self, command: str, handler: CommandHandler) -> None:
        self.handlers[command] = handler

    async def dispatch(self, text: str) -> str | None:
        text = text.strip()
        if not text.startswith("/"):
            return None
        command, _, args = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        handler = self.handlers.get(command)
        if handler is None:
            known = " ".join(sorted(self.handlers))
            return f"Unknown command {command}. Available: {known}"
        try:
            return await handler(args.strip())
        except Exception:
            log.exception("command handler %s failed", command)
            return f"Command {command} failed — see logs."


class TelegramBot(BaseCollector):
    """Long-polling loop serving both control commands and source ingestion."""

    name = "telegram_bot"
    source_type = SourceType.TELEGRAM

    def __init__(
        self,
        client: TelegramClient,
        *,
        admin_chat_id: str,
        router: CommandRouter,
        ingest_handler: Callable[[dict], Awaitable[None]] | None = None,
        ingest_chat_ids: set[str] | None = None,
        poll_timeout_s: int = 50,
    ) -> None:
        super().__init__()
        self.client = client
        self.admin_chat_id = str(admin_chat_id)
        self.router = router
        self.ingest_handler = ingest_handler
        self.ingest_chat_ids = {str(c) for c in (ingest_chat_ids or set())}
        self.poll_timeout_s = poll_timeout_s
        self._offset: int | None = None

    async def run(self) -> None:
        self.health.healthy = True
        self.health.detail = "polling"
        while True:
            updates = await self.client.get_updates(self._offset, timeout_s=self.poll_timeout_s)
            for update in updates:
                self._offset = update["update_id"] + 1
                await self.handle_update(update)
                self.health.mark_event()

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = message.get("text") or message.get("caption") or ""
        if not chat_id or not text:
            return
        if chat_id == self.admin_chat_id:
            reply = await self.router.dispatch(text)
            if reply:
                try:
                    await self.client.send_message(chat_id, reply)
                except ConnectionError:
                    log.warning("failed to send command reply")
        elif chat_id in self.ingest_chat_ids and self.ingest_handler is not None:
            # LOW TRUST ingestion path — parsed as data, never as commands
            await self.ingest_handler(message)
        # all other chats: ignored by design
