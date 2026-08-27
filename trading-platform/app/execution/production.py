"""Production executor — PERMANENTLY DISABLED.

This class exists so the architecture has an explicit, auditable answer to
"what happens if production execution is attempted": it raises
`ProductionExecutionDisabled`. Always. There is no flag, environment variable,
configuration, or subclass that changes this. Do not "fix" this module.

TradeIntents for production remain hypothetical objects: they are persisted,
displayed and paper-/testnet-executed only (see ARCHITECTURE.md, SECURITY.md).
"""

from __future__ import annotations

from typing import Any, NoReturn

from app.core.errors import ProductionExecutionDisabled


class ProductionExecutor:
    """Sealed stub. Every trading method raises ProductionExecutionDisabled."""

    ENABLED = False  # constant; nothing reads or writes this to change behavior

    def __init_subclass__(cls, **kwargs: Any) -> NoReturn:
        raise ProductionExecutionDisabled(
            "ProductionExecutor cannot be subclassed to enable execution")

    def _disabled(self, action: str) -> NoReturn:
        raise ProductionExecutionDisabled(f"attempted production {action}")

    async def submit(self, intent: Any) -> NoReturn:
        self._disabled("order submission")

    async def place_order(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._disabled("order placement")

    async def cancel_order(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._disabled("order cancellation")

    async def cancel_all(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._disabled("bulk order cancellation")

    async def close_position(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._disabled("position close order")
