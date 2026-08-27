"""Task supervisor: crash isolation with exponential-backoff restart.

A failing collector/service never brings down the process; it is logged, its
component is marked degraded, and it restarts with backoff.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger, log_ctx
from app.core.state import StateMachine

log = get_logger(__name__)


@dataclass
class Supervisor:
    state: StateMachine | None = None
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _stopping: bool = False

    def start(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        max_backoff: float = 120.0,
        mark_degraded: bool = True,
    ) -> None:
        if name in self._tasks:
            raise ValueError(f"task {name!r} already supervised")
        self._tasks[name] = asyncio.create_task(
            self._run(name, factory, max_backoff, mark_degraded), name=f"supervised:{name}"
        )

    async def _run(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        max_backoff: float,
        mark_degraded: bool,
    ) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                if self.state and mark_degraded:
                    self.state.set_component_degraded(name, False)
                await factory()
                if self._stopping:
                    return
                log_ctx(log, logging.WARNING, "supervised task exited; restarting", task=name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("supervised task %s crashed", name)
                if self.state and mark_degraded:
                    self.state.set_component_degraded(name, True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
