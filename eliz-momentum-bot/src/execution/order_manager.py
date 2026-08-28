"""Order manager: persist-before-send, duplicate prevention, UNKNOWN reconcile."""

from __future__ import annotations

from src.core.clock import Clock
from src.core.domain import BookTop, OrderIntent, OrderResult, OrderStatus
from src.core.logger import get_logger
from src.execution.adapters import ExecutionAdapter, client_order_id
from src.storage.database import Repo

log = get_logger(__name__)


class OrderManager:
    def __init__(self, adapter: ExecutionAdapter, repo: Repo, clock: Clock) -> None:
        self.adapter = adapter
        self.repo = repo
        self.clock = clock

    async def submit(self, intent: OrderIntent, *, book: BookTop | None) -> OrderResult:
        coid = client_order_id(intent.id)
        existing = await self.repo.get_order_by_client_id(coid)
        if existing is not None:
            # duplicate submission attempt (retry after crash) → never resend blindly
            log.warning("duplicate order intent %s blocked (status %s)",
                        coid, existing.status)
            return OrderResult(intent_id=intent.id, client_order_id=coid,
                               status=OrderStatus(existing.status),
                               error="duplicate intent; original preserved")
        requested = None
        if book is not None:
            requested = book.ask_price if intent.side.value == "BUY" else book.bid_price
        await self.repo.store_order_intent(intent, coid, requested, self.clock.now())
        result = await self.adapter.execute(intent, book=book)
        await self.repo.update_order_result(result, self.clock.now())
        if result.status == OrderStatus.UNKNOWN:
            log.warning("order %s outcome UNKNOWN — reconcile before any retry", coid)
        return result

    async def reconcile_unknown(self, live_client=None) -> int:
        """Resolve UNKNOWN/PENDING orders against the exchange (live mode only)."""
        rows = await self.repo.orders_with_status(
            [OrderStatus.UNKNOWN.value, OrderStatus.PENDING_SUBMIT.value])
        resolved = 0
        for row in rows:
            if live_client is None:
                # paper mode: a pending row after crash simply never executed
                await self.repo.update_order_result(
                    OrderResult(intent_id=row.id, client_order_id=row.client_order_id,
                                status=OrderStatus.REJECTED,
                                error="unresolved at restart (paper)"), self.clock.now())
                resolved += 1
                continue
            remote = await live_client.query_order(row.symbol, row.client_order_id)
            if remote is None:
                await self.repo.update_order_result(
                    OrderResult(intent_id=row.id, client_order_id=row.client_order_id,
                                status=OrderStatus.REJECTED,
                                error="not found on exchange during reconcile"),
                    self.clock.now())
            else:
                from decimal import Decimal

                from src.execution.adapters import LiveAdapter

                mapped = LiveAdapter._map_response(
                    intent=OrderIntentShim(row), coid=row.client_order_id, resp=remote,
                    requested=row.requested_price, latency_ms=0.0)
                mapped = mapped.model_copy(update={
                    "executed_qty": Decimal(str(remote.get("executedQty", "0")))})
                await self.repo.update_order_result(mapped, self.clock.now())
            resolved += 1
        return resolved


class OrderIntentShim:
    """Minimal duck-typed intent for reconcile mapping (row → intent fields)."""

    def __init__(self, row) -> None:
        self.id = row.id
        self.symbol = row.symbol


