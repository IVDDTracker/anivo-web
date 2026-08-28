"""Per-signal trade session state machine (spec §6/§9).

Transitions are validated: an illegal transition raises, so a logic bug can
never silently produce e.g. a SHORT without the long leg having exited.
Every transition is timestamped and can be persisted via the on_transition hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum


class TradeState(StrEnum):
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    MARKET_VALIDATION = "MARKET_VALIDATION"
    ENTRY_APPROVED = "ENTRY_APPROVED"
    MONITORING_PUMP = "MONITORING_PUMP"          # SHORT_ONLY: wait for pump + reversal
    LONG_OPEN = "LONG_OPEN"
    LONG_EXIT = "LONG_EXIT"
    WAITING_SHORT_CONFIRMATION = "WAITING_SHORT_CONFIRMATION"
    SHORT_OPEN = "SHORT_OPEN"
    SHORT_EXIT = "SHORT_EXIT"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    ABORTED = "ABORTED"


_ALLOWED: dict[TradeState, set[TradeState]] = {
    TradeState.SIGNAL_DETECTED: {TradeState.MARKET_VALIDATION, TradeState.SKIPPED},
    TradeState.MARKET_VALIDATION: {TradeState.ENTRY_APPROVED, TradeState.MONITORING_PUMP,
                                   TradeState.SKIPPED},
    TradeState.MONITORING_PUMP: {TradeState.WAITING_SHORT_CONFIRMATION, TradeState.DONE,
                                 TradeState.SKIPPED, TradeState.ABORTED},
    TradeState.ENTRY_APPROVED: {TradeState.LONG_OPEN, TradeState.SKIPPED, TradeState.ABORTED},
    TradeState.LONG_OPEN: {TradeState.LONG_EXIT, TradeState.ABORTED},
    TradeState.LONG_EXIT: {TradeState.WAITING_SHORT_CONFIRMATION, TradeState.DONE,
                           TradeState.ABORTED},
    TradeState.WAITING_SHORT_CONFIRMATION: {TradeState.SHORT_OPEN, TradeState.DONE,
                                            TradeState.ABORTED},
    TradeState.SHORT_OPEN: {TradeState.SHORT_EXIT, TradeState.ABORTED},
    TradeState.SHORT_EXIT: {TradeState.DONE, TradeState.ABORTED},
    TradeState.DONE: set(),
    TradeState.SKIPPED: set(),
    TradeState.ABORTED: set(),
}

TERMINAL = {TradeState.DONE, TradeState.SKIPPED, TradeState.ABORTED}


class IllegalTransition(RuntimeError):
    pass


class TradeStateMachine:
    def __init__(self, session_id: str,
                 on_transition: Callable[[str, TradeState, TradeState, str, datetime],
                                         Awaitable[None]] | None = None) -> None:
        self.session_id = session_id
        self.state = TradeState.SIGNAL_DETECTED
        self.history: list[tuple[TradeState, str, datetime]] = []
        self._hook = on_transition

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    async def to(self, new: TradeState, reason: str, now: datetime) -> None:
        if new not in _ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state} → {new} ({reason})")
        old, self.state = self.state, new
        self.history.append((new, reason, now))
        if self._hook is not None:
            await self._hook(self.session_id, old, new, reason, now)
