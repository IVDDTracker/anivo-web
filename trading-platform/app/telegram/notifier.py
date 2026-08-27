"""Telegram notifier: queued, throttled, deduplicated operator notifications."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.core.logging import get_logger
from app.models.enums import Regime, Venue
from app.models.orders import Position
from app.models.signals import DecisionRecord, Signal
from app.telegram.client import TelegramClient

log = get_logger(__name__)


@dataclass
class Notifier:
    client: TelegramClient | None
    chat_id: str
    clock: Clock
    dedupe_window_s: float = 300.0
    _queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    _recent: dict[str, datetime] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.client is not None and bool(self.chat_id)

    async def run(self) -> None:
        """Drains the queue; supervised task."""
        if not self.enabled:
            return
        while True:
            text = await self._queue.get()
            try:
                await self.client.send_message(self.chat_id, text)
            except ConnectionError:
                log.warning("notify send failed; message dropped after retry")
                try:
                    await asyncio.sleep(3)
                    await self.client.send_message(self.chat_id, text)
                except ConnectionError:
                    pass

    def _enqueue(self, key: str, text: str) -> None:
        if not self.enabled:
            return
        now = self.clock.now()
        last = self._recent.get(key)
        if last is not None and (now - last) < timedelta(seconds=self.dedupe_window_s):
            return
        self._recent[key] = now
        if len(self._recent) > 2000:
            cutoff = now - timedelta(seconds=self.dedupe_window_s)
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("notification queue full; dropping oldest")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(text)
            except asyncio.QueueEmpty:
                pass

    # ── formatted notifications ──────────────────────────────────────────────

    def signal_detected(self, signal: Signal, decision: DecisionRecord, status_line: str) -> None:
        evidence = "\n".join(f"• {e.name}: {e.value}" for e in signal.evidence[:8]) or "• (none)"
        risks = "\n".join(f"• {r}" for r in signal.risks[:5]) or "• none identified"
        fusion = decision.fusion.final_score if decision.fusion else signal.confidence
        text = (
            f"🔔 SIGNAL DETECTED\n\n{signal.symbol}\n\n"
            f"Direction: {signal.direction}\n"
            f"Confidence: {fusion:.0f}/100\n"
            f"Market regime: {signal.market_regime}\n"
            f"Reference: {signal.reference_price:.6g}"
            + (f" | Stop: {signal.hypothetical_stop:.6g}" if signal.hypothetical_stop else "")
            + (f" | Target: {signal.hypothetical_target:.6g}" if signal.hypothetical_target else "")
            + f"\n\nEvidence:\n{evidence}\n\nRisks:\n{risks}\n\n"
            f"Strategy: {signal.strategy} v{signal.strategy_version}\n"
            f"Status: {status_line}"
        )
        self._enqueue(f"signal:{signal.id}", text)

    def position_opened(self, pos: Position) -> None:
        venue = "PAPER" if pos.venue == Venue.PAPER else "TESTNET"
        self._enqueue(
            f"pos_open:{pos.id}",
            f"📈 {venue} POSITION OPENED\n{pos.symbol} {pos.direction}\n"
            f"qty {pos.qty} @ {pos.avg_entry_price}\n"
            f"stop {pos.stop_price} | target {pos.target_price}\n"
            f"strategy {pos.strategy}",
        )

    def position_closed(self, pos: Position) -> None:
        venue = "PAPER" if pos.venue == Venue.PAPER else "TESTNET"
        emoji = "✅" if pos.realized_pnl >= 0 else "🔻"
        self._enqueue(
            f"pos_close:{pos.id}",
            f"{emoji} {venue} POSITION CLOSED\n{pos.symbol}\n"
            f"PnL {pos.realized_pnl:.2f} (fees {pos.fees_paid:.2f})\n"
            f"reason: {pos.close_reason}\nstrategy {pos.strategy}",
        )

    def risk_alert(self, kind: str, detail: str) -> None:
        self._enqueue(f"risk:{kind}:{detail[:40]}", f"🛑 RISK: {kind}\n{detail}")

    def system_alert(self, kind: str, detail: str) -> None:
        self._enqueue(f"sys:{kind}:{detail[:40]}", f"⚠️ SYSTEM: {kind}\n{detail}")

    def source_unhealthy(self, source: str, detail: str) -> None:
        self._enqueue(f"src:{source}", f"📡 DATA FEED UNHEALTHY: {source}\n{detail}")

    def regime_change(self, symbol: str, old: Regime | str, new: Regime | str) -> None:
        self._enqueue(f"regime:{symbol}:{new}", f"🌊 REGIME CHANGE {symbol}: {old} → {new}")

    def strategy_degraded(self, strategy: str, detail: str) -> None:
        self._enqueue(f"degraded:{strategy}", f"📉 STRATEGY DEGRADED: {strategy}\n{detail}")

    def external_event(self, headline: str, source: str, confidence: float, assets: list[str]) -> None:
        self._enqueue(
            f"ext:{headline[:60]}",
            f"📰 SIGNIFICANT EVENT ({', '.join(assets) or 'market'})\n{headline}\n"
            f"source: {source} | confidence {confidence:.2f}",
        )

    def report(self, text: str) -> None:
        self._enqueue(f"report:{self.clock.now().isoformat()}", text)
