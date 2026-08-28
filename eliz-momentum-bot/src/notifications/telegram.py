"""Telegram notifications (spec §16). Failures never break trading."""

from __future__ import annotations

import asyncio

import httpx

from src.core.logger import get_logger, register_secret

log = get_logger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *,
                 client: httpx.AsyncClient | None = None) -> None:
        register_secret(token)
        self.enabled = bool(token and chat_id)
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._lock = asyncio.Lock()

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        async with self._lock:  # keep ordering, ~1 msg/s per chat
            try:
                resp = await self._client.post(self._url, json={
                    "chat_id": self.chat_id, "text": text[:4096]})
                if resp.status_code == 429:
                    retry = float((resp.json().get("parameters") or {}).get("retry_after", 3))
                    await asyncio.sleep(min(retry, 30))
                    await self._client.post(self._url, json={
                        "chat_id": self.chat_id, "text": text[:4096]})
            except httpx.HTTPError as exc:
                log.warning("telegram send failed: %s", str(exc)[:150])
            await asyncio.sleep(1.05)

    # ── event formats (spec §16) ─────────────────────────────────────────────

    async def new_tweet(self, tweet, classification) -> None:
        stage = classification.signal_stage.value if classification.is_trade_signal else "—"
        await self.send(
            f"🐦 NEW TWEET @{'' or 'eliz883'}\n"
            f"“{tweet.text[:200]}”\n"
            f"latency: {tweet.latency_ms / 1000:.1f}s | signal: "
            f"{'YES' if classification.is_trade_signal else 'no'} ({stage}) "
            f"conf {classification.confidence:.2f}")

    async def signal_detected(self, tweet, classification, symbol: str) -> None:
        await self.send(
            f"🚨 ELIZ SIGNAL\n\nCoin: {classification.symbol}\n"
            f"Tweet: “{tweet.text[:200]}”\n"
            f"Tweet latency: {tweet.latency_ms / 1000:.1f}s\n"
            f"Stage: {classification.signal_stage.value} | "
            f"conf {classification.confidence:.2f}\n"
            f"Futures symbol: {symbol}")

    async def long_opened(self, session, result, inputs) -> None:
        move = (inputs.mid_price / inputs.reference_price - 1.0) * 100.0
        await self.send(
            f"📈 LONG OPENED — {session.symbol}\n"
            f"Reference: {inputs.reference_price:.6g}\n"
            f"Entry: {result.executed_price:.6g}\n"
            f"Move before entry: {move:+.2f}%\n"
            f"Qty: {result.executed_qty} "
            f"(~{float(result.executed_qty) * result.executed_price:.0f} USDT)\n"
            f"Slippage: {result.slippage_pct or 0:.3f}% | "
            f"total latency {session.latencies.get('total_signal_to_order_latency_ms', 0) / 1000:.1f}s")

    async def long_closed(self, session, result, net: float, reason: str) -> None:
        emoji = "✅" if net >= 0 else "🔻"
        await self.send(
            f"{emoji} LONG CLOSED — {session.symbol}\n"
            f"Exit: {result.executed_price:.6g}\nReason: {reason}\n"
            f"Net PnL: {net:+.2f} USDT")

    async def short_opened(self, session, result) -> None:
        await self.send(
            f"📉 SHORT OPENED — {session.symbol}\n"
            f"Entry: {result.executed_price:.6g}\n"
            f"Qty: {result.executed_qty}\n"
            f"(reversal confirmed after pump)")

    async def short_closed(self, session, result, net: float, reason: str) -> None:
        emoji = "✅" if net >= 0 else "🔻"
        await self.send(
            f"{emoji} SHORT CLOSED — {session.symbol}\n"
            f"Exit: {result.executed_price:.6g}\nReason: {reason}\n"
            f"Net PnL: {net:+.2f} USDT")

    async def skipped(self, session, reason, detail: str) -> None:
        await self.send(f"⏭ SKIPPED — {session.symbol}\n{reason}: {detail[:200]}")

    async def kill_switch(self, reasons: dict) -> None:
        lines = "\n".join(f"• {k}: {v}" for k, v in reasons.items())
        await self.send(f"🛑 KILL SWITCH ACTIVE\n{lines}\nNo new trades will open.")

    async def session_done(self, session) -> None:
        return  # per-leg messages already tell the story; keep the channel quiet
