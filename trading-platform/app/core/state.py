"""System state machine.

Explicit states with explicit gates. `can_open_new_positions` is the single
authority the pipeline consults; it is fail-safe (any lock/pause/staleness → False).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.clock import Clock
from app.models.enums import ExecutionMode, SystemState


@dataclass
class SystemStatus:
    state: SystemState
    execution_mode: ExecutionMode
    paused: bool
    risk_locked: bool
    stale_symbols: list[str]
    reason: str


@dataclass
class StateMachine:
    clock: Clock
    execution_mode: ExecutionMode = ExecutionMode.PAPER_ONLY
    _state: SystemState = SystemState.STARTING
    _paused: bool = False
    _risk_locked: bool = False
    _risk_lock_reason: str = ""
    _degraded_components: set[str] = field(default_factory=set)
    _stale_symbols: set[str] = field(default_factory=set)
    _changed_at: datetime | None = None

    def _recompute(self) -> None:
        prev = self._state
        if self._risk_locked:
            new = SystemState.RISK_LOCK
        elif self._paused:
            new = SystemState.PAUSED
        elif self._stale_symbols:
            new = SystemState.DATA_STALE
        elif self._degraded_components:
            new = SystemState.DEGRADED
        elif self._state == SystemState.STARTING:
            new = SystemState.STARTING
        else:
            new = SystemState.HEALTHY
        if new != prev:
            self._state = new
            self._changed_at = self.clock.now()

    def mark_started(self) -> None:
        if self._state == SystemState.STARTING:
            self._state = SystemState.HEALTHY
            self._changed_at = self.clock.now()
        self._recompute()

    def pause(self) -> None:
        self._paused = True
        self._recompute()

    def resume(self) -> None:
        self._paused = False
        self._recompute()

    def risk_lock(self, reason: str) -> None:
        self._risk_locked = True
        self._risk_lock_reason = reason
        self._recompute()

    def risk_unlock(self) -> None:
        self._risk_locked = False
        self._risk_lock_reason = ""
        self._recompute()

    def set_component_degraded(self, component: str, degraded: bool) -> None:
        if degraded:
            self._degraded_components.add(component)
        else:
            self._degraded_components.discard(component)
        self._recompute()

    def set_symbol_stale(self, symbol: str, stale: bool) -> None:
        if stale:
            self._stale_symbols.add(symbol)
        else:
            self._stale_symbols.discard(symbol)
        self._recompute()

    @property
    def state(self) -> SystemState:
        return self._state

    def can_open_new_positions(self, symbol: str | None = None) -> tuple[bool, str]:
        """Fail-safe gate. Only an affirmatively healthy system may open positions."""
        if self._state == SystemState.STARTING:
            return False, "system still starting"
        if self._risk_locked:
            return False, f"risk lock active: {self._risk_lock_reason}"
        if self._paused:
            return False, "system paused by operator"
        if symbol is not None and symbol in self._stale_symbols:
            return False, f"market data stale for {symbol}"
        if symbol is None and self._stale_symbols:
            return False, f"market data stale: {sorted(self._stale_symbols)}"
        if self._state not in (SystemState.HEALTHY, SystemState.DEGRADED):
            return False, f"system state {self._state}"
        if self._state == SystemState.DEGRADED:
            # Degraded still blocks new entries; exits are handled elsewhere and stay allowed.
            return False, f"degraded components: {sorted(self._degraded_components)}"
        return True, "ok"

    def status(self) -> SystemStatus:
        reason = self._risk_lock_reason if self._risk_locked else ""
        if self._degraded_components:
            reason = f"degraded: {sorted(self._degraded_components)}"
        return SystemStatus(
            state=self._state,
            execution_mode=self.execution_mode,
            paused=self._paused,
            risk_locked=self._risk_locked,
            stale_symbols=sorted(self._stale_symbols),
            reason=reason,
        )
