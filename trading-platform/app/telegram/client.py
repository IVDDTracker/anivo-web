"""Thin Telegram Bot API client (official HTTP API, long polling).

The bot token is a secret: it is registered with the log masker and any exception
text is scrubbed before propagating.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.logging import get_logger, register_secret

log = get_logger(__name__)


class TelegramClient:
    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org",
                 client: httpx.AsyncClient | None = None,
                 min_send_interval_s: float = 1.1) -> None:
        self._token = token
        register_secret(token)
        self._base = f"{api_base}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=65.0)
        self._min_interval = min_send_interval_s
        self._last_send: dict[str, float] = {}
        self._send_lock = asyncio.Lock()

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***TOKEN***")

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            resp = await self._client.post(f"{self._base}/{method}", json=params)
        except httpx.HTTPError as exc:
            raise ConnectionError(f"telegram {method} failed: {self._scrub(str(exc))}") from None
        data = resp.json() if resp.content else {}
        if resp.status_code == 429:
            retry = float((data.get("parameters") or {}).get("retry_after", 5))
            log.warning("telegram rate limited; sleeping %.1fs", retry)
            await asyncio.sleep(retry)
            raise ConnectionError("telegram rate limited")
        if not data.get("ok", False):
            raise ConnectionError(
                f"telegram {method} error {resp.status_code}: "
                f"{self._scrub(str(data.get('description', ''))[:200])}")
        return data["result"]

    async def get_updates(self, offset: int | None, timeout_s: int = 50) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout_s,
                                  "allowed_updates": ["message", "channel_post"]}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params)

    async def send_message(self, chat_id: str, text: str, *, silent: bool = False) -> None:
        loop = asyncio.get_running_loop()
        async with self._send_lock:
            last = self._last_send.get(chat_id, 0.0)
            wait = self._min_interval - (loop.time() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send[chat_id] = loop.time()
        await self._call("sendMessage", chat_id=chat_id, text=text[:4096],
                         disable_notification=silent)

    async def healthcheck(self) -> bool:
        try:
            await self._call("getMe")
            return True
        except ConnectionError:
            return False

    async def close(self) -> None:
        await self._client.aclose()
