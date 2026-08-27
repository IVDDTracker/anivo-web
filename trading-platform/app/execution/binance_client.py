"""Signed Binance REST client (HMAC SHA256 per official rest-api.md).

SAFETY LAYER (SECURITY.md layer 2): this transport REFUSES to send any
order-mutating request to a non-testnet host, regardless of caller. Production
credentials can therefore only ever be used for read-only endpoints.

Unknown-outcome semantics per official docs: HTTP 5xx and -1007 TIMEOUT mean the
request MAY have executed — mutating calls raise OrderOutcomeUnknown and must be
reconciled by clientOrderId, never blindly retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.core.errors import ExchangeError, OrderOutcomeUnknown, ProductionExecutionDisabled
from app.core.logging import get_logger, register_secret

log = get_logger(__name__)

TESTNET_HOSTS = ("testnet.binance.vision",)

# endpoints that mutate orders (any non-GET method)
_ORDER_MUTATING_PREFIXES = ("/api/v3/order", "/api/v3/sor", "/api/v3/openorders")


def is_order_mutating(method: str, path: str) -> bool:
    return method.upper() != "GET" and path.lower().startswith(_ORDER_MUTATING_PREFIXES)


class BinanceSignedClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str, *,
                 recv_window_ms: int = 5000, timeout_s: float = 10.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        # official testnet docs quote the base as https://testnet.binance.vision/api;
        # our paths already carry /api/v3, so normalize away a trailing /api
        if self.base_url.endswith("/api"):
            self.base_url = self.base_url[: -len("/api")]
        self._host = urlparse(self.base_url).hostname or ""
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        register_secret(api_key)
        register_secret(api_secret)
        self.recv_window_ms = recv_window_ms
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    @property
    def is_testnet(self) -> bool:
        return self._host in TESTNET_HOSTS

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params)
        signature = hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    async def signed_request(self, method: str, path: str,
                             params: dict[str, Any] | None = None) -> dict | list:
        # ── HARD SAFETY GATE: production order execution is disabled by design ──
        if is_order_mutating(method, path) and not self.is_testnet:
            raise ProductionExecutionDisabled(
                f"refused {method} {path} against {self._host}")
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window_ms
        payload = self._sign(params)
        url = f"{self.base_url}{path}?{payload}"
        headers = {"X-MBX-APIKEY": self._api_key}
        mutating = is_order_mutating(method, path)
        try:
            resp = await self._client.request(method, url, headers=headers)
        except httpx.TimeoutException as exc:
            if mutating:
                raise OrderOutcomeUnknown(f"timeout on {method} {path}") from exc
            raise ExchangeError(f"timeout on {method} {path}") from exc
        except httpx.HTTPError as exc:
            if mutating:
                raise OrderOutcomeUnknown(f"transport error on {method} {path}") from exc
            raise ExchangeError(f"transport error on {method} {path}: {exc}") from exc

        if resp.status_code in (429, 418):
            retry_after = float(resp.headers.get("Retry-After", "10"))
            log.warning("binance signed API rate limited; backing off %.0fs", retry_after)
            await asyncio.sleep(min(retry_after, 60))
            raise ExchangeError("rate limited", http_status=resp.status_code)
        if resp.status_code >= 500:
            if mutating:
                raise OrderOutcomeUnknown(f"HTTP {resp.status_code} on {method} {path}")
            raise ExchangeError(f"HTTP {resp.status_code}", http_status=resp.status_code)

        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg", "") if isinstance(data, dict) else str(data)[:200]
            if code == -1007 and mutating:
                raise OrderOutcomeUnknown(f"-1007 TIMEOUT on {method} {path}")
            raise ExchangeError(f"binance error {code}: {msg}", code=code,
                                http_status=resp.status_code)
        return data

    async def public_request(self, path: str, params: dict[str, Any] | None = None) -> dict | list:
        resp = await self._client.get(f"{self.base_url}{path}", params=params or {})
        if resp.status_code >= 400:
            raise ExchangeError(f"HTTP {resp.status_code} on {path}", http_status=resp.status_code)
        return resp.json()

    # ── typed helpers ────────────────────────────────────────────────────────

    async def new_order(self, *, symbol: str, side: str, order_type: str, quantity: str,
                        price: str | None = None, time_in_force: str | None = None,
                        client_order_id: str) -> dict:
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": order_type, "quantity": quantity,
            "newClientOrderId": client_order_id, "newOrderRespType": "FULL",
        }
        if price is not None:
            params["price"] = price
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        result = await self.signed_request("POST", "/api/v3/order", params)
        assert isinstance(result, dict)
        return result

    async def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict:
        result = await self.signed_request("DELETE", "/api/v3/order", {
            "symbol": symbol, "origClientOrderId": orig_client_order_id})
        assert isinstance(result, dict)
        return result

    async def get_order(self, *, symbol: str, orig_client_order_id: str) -> dict | None:
        """Returns the order or None when the exchange does not know it (-2013)."""
        try:
            result = await self.signed_request("GET", "/api/v3/order", {
                "symbol": symbol, "origClientOrderId": orig_client_order_id})
        except ExchangeError as exc:
            if exc.code == -2013:  # Order does not exist
                return None
            raise
        assert isinstance(result, dict)
        return result

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        result = await self.signed_request("GET", "/api/v3/openOrders", params)
        assert isinstance(result, list)
        return result

    async def account(self) -> dict:
        result = await self.signed_request("GET", "/api/v3/account")
        assert isinstance(result, dict)
        return result

    async def close(self) -> None:
        await self._client.aclose()
