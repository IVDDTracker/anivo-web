"""Binance USDⓈ-M Futures REST client (endpoints verified against the official
binance-futures-connector source: base https://fapi.binance.com, /fapi/v1/*).

Safety layers:
- order-mutating signed calls require the client to be constructed with
  `allow_trading=True`; only the live execution adapter does that, and it in
  turn requires BOTH safety flags (MODE=LIVE + ENABLE_LIVE_TRADING) — a paper
  run can therefore never place a real order even if credentials are present;
- timeout / 5xx / -1007 on an order call raise OrderOutcomeUnknown: the caller
  must reconcile by client order id before ANY retry (no duplicate orders);
- 429/418 honor Retry-After and back off.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from src.core.logger import get_logger, register_secret

log = get_logger(__name__)


class ExchangeError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class OrderOutcomeUnknown(RuntimeError):
    """Order may or may not exist on the exchange — reconcile before retrying."""


class LiveTradingDisabled(RuntimeError):
    """Raised when an order-mutating call is attempted without trading enabled."""


_MUTATING_PATHS = ("/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v1/allOpenOrders",
                   "/fapi/v1/leverage", "/fapi/v1/marginType")


class BinanceFuturesClient:
    def __init__(self, base_url: str, *, api_key: str = "", api_secret: str = "",
                 allow_trading: bool = False, recv_window_ms: int = 5000,
                 timeout_s: float = 10.0, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode() if api_secret else b""
        self.allow_trading = allow_trading
        self.recv_window_ms = recv_window_ms
        register_secret(api_key)
        register_secret(api_secret)
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    # ── transport ────────────────────────────────────────────────────────────

    async def _handle_status(self, resp: httpx.Response, *, mutating: bool) -> dict | list:
        if resp.status_code in (429, 418):
            retry_after = float(resp.headers.get("Retry-After", "10"))
            log.warning("binance futures rate limited (%s); backing off %.0fs",
                        resp.status_code, retry_after)
            await asyncio.sleep(min(retry_after, 60))
            raise ExchangeError("rate limited", status=resp.status_code)
        if resp.status_code >= 500:
            if mutating:
                raise OrderOutcomeUnknown(f"HTTP {resp.status_code} on order call")
            raise ExchangeError(f"HTTP {resp.status_code}", status=resp.status_code)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg", "") if isinstance(data, dict) else str(data)[:200]
            if code == -1007 and mutating:
                raise OrderOutcomeUnknown("-1007 timeout: status unknown")
            raise ExchangeError(f"binance error {code}: {msg}", code=code,
                                status=resp.status_code)
        return data

    async def public(self, path: str, params: dict | None = None) -> dict | list:
        try:
            resp = await self._client.get(f"{self.base_url}{path}", params=params or {})
        except httpx.HTTPError as exc:
            raise ExchangeError(f"transport error on {path}: {exc}") from exc
        return await self._handle_status(resp, mutating=False)

    async def signed(self, method: str, path: str, params: dict | None = None) -> dict | list:
        if not self._api_key or not self._api_secret:
            raise ExchangeError("signed call without API credentials")
        mutating = method != "GET" and path.startswith(_MUTATING_PATHS)
        if mutating and not self.allow_trading:
            # spec §13 double-flag safety: only the live adapter constructs a
            # trading-enabled client, and only when MODE=LIVE + ENABLE_LIVE_TRADING
            raise LiveTradingDisabled(
                f"refused {method} {path}: trading not enabled on this client")
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = self.recv_window_ms
        query = urlencode(p)
        signature = hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{query}&signature={signature}"
        try:
            resp = await self._client.request(method, url,
                                              headers={"X-MBX-APIKEY": self._api_key})
        except httpx.TimeoutException as exc:
            if mutating:
                raise OrderOutcomeUnknown(f"timeout on {method} {path}") from exc
            raise ExchangeError(f"timeout on {path}") from exc
        except httpx.HTTPError as exc:
            if mutating:
                raise OrderOutcomeUnknown(f"transport error on {method} {path}") from exc
            raise ExchangeError(f"transport error on {path}: {exc}") from exc
        return await self._handle_status(resp, mutating=mutating)

    # ── public market data ───────────────────────────────────────────────────

    async def ping(self) -> bool:
        try:
            await self.public("/fapi/v1/ping")
            return True
        except ExchangeError:
            return False

    async def exchange_info(self) -> dict:
        data = await self.public("/fapi/v1/exchangeInfo")
        assert isinstance(data, dict)
        return data

    async def book_ticker(self, symbol: str) -> dict:
        data = await self.public("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        assert isinstance(data, dict)
        return data

    async def depth(self, symbol: str, limit: int = 20) -> dict:
        data = await self.public("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        assert isinstance(data, dict)
        return data

    async def ticker_24h(self, symbol: str) -> dict:
        data = await self.public("/fapi/v1/ticker/24hr", {"symbol": symbol})
        assert isinstance(data, dict)
        return data

    async def agg_trades(self, symbol: str, *, start_ms: int | None = None,
                         end_ms: int | None = None, from_id: int | None = None,
                         limit: int = 1000) -> list[dict]:
        params: dict[str, Any] = {"symbol": symbol, "limit": min(limit, 1000)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        if from_id is not None:
            params["fromId"] = from_id
        data = await self.public("/fapi/v1/aggTrades", params)
        assert isinstance(data, list)
        return data

    async def klines(self, symbol: str, interval: str = "1m", *, limit: int = 500,
                     start_ms: int | None = None, end_ms: int | None = None) -> list[list]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval,
                                  "limit": min(limit, 1500)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        data = await self.public("/fapi/v1/klines", params)
        assert isinstance(data, list)
        return data

    # ── signed account / trading ─────────────────────────────────────────────

    async def balance(self) -> list[dict]:
        data = await self.signed("GET", "/fapi/v3/balance")
        assert isinstance(data, list)
        return data

    async def position_risk(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        data = await self.signed("GET", "/fapi/v3/positionRisk", params)
        assert isinstance(data, list)
        return data

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        data = await self.signed("POST", "/fapi/v1/leverage",
                                 {"symbol": symbol, "leverage": leverage})
        assert isinstance(data, dict)
        return data

    async def new_order(self, *, symbol: str, side: str, order_type: str, quantity: str,
                        client_order_id: str, price: str | None = None,
                        time_in_force: str | None = None,
                        reduce_only: bool = False) -> dict:
        params: dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type,
                                  "quantity": quantity,
                                  "newClientOrderId": client_order_id,
                                  "newOrderRespType": "RESULT"}
        if price is not None:
            params["price"] = price
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        data = await self.signed("POST", "/fapi/v1/order", params)
        assert isinstance(data, dict)
        return data

    async def query_order(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            data = await self.signed("GET", "/fapi/v1/order",
                                     {"symbol": symbol,
                                      "origClientOrderId": client_order_id})
        except ExchangeError as exc:
            if exc.code == -2013:  # order does not exist
                return None
            raise
        assert isinstance(data, dict)
        return data

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            data = await self.signed("DELETE", "/fapi/v1/order",
                                     {"symbol": symbol,
                                      "origClientOrderId": client_order_id})
        except ExchangeError as exc:
            if exc.code == -2011:  # unknown order (already gone)
                return None
            raise
        assert isinstance(data, dict)
        return data

    async def close(self) -> None:
        await self._client.aclose()
