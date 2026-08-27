"""Binance public market-data REST client (data-api.binance.vision — no API key).

Rate-limit handling per official docs: limits come from exchangeInfo `rateLimits`;
every response's X-MBX-USED-WEIGHT-* headers are tracked; 429/418 honor Retry-After.
"""

from __future__ import annotations

import asyncio

import httpx

from app.core.errors import ExchangeError
from app.core.logging import get_logger
from app.data.normalization.binance import parse_rest_kline, parse_symbol_rules
from app.models.market import Candle, SymbolRules

log = get_logger(__name__)


class RateLimitTracker:
    def __init__(self, weight_per_minute: int) -> None:
        self.limit = weight_per_minute
        self.used = 0
        self.retry_after_s: float = 0.0

    def update_from_headers(self, headers: httpx.Headers) -> None:
        for key, value in headers.items():
            if key.lower().startswith("x-mbx-used-weight-1m"):
                try:
                    self.used = int(value)
                except ValueError:
                    pass

    @property
    def nearly_exhausted(self) -> bool:
        return self.used >= 0.9 * self.limit


class BinanceMarketData:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0,
                 fallback_weight_per_minute: int = 6000,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)
        self.rate = RateLimitTracker(fallback_weight_per_minute)

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        if self.rate.nearly_exhausted:
            log.warning("rate limit budget nearly exhausted; backing off 5s")
            await asyncio.sleep(5)
        resp = await self._client.get(path, params=params)
        self.rate.update_from_headers(resp.headers)
        if resp.status_code in (429, 418):
            retry_after = float(resp.headers.get("Retry-After", "10"))
            log.warning("binance rate limited (%s); sleeping %ss", resp.status_code, retry_after)
            await asyncio.sleep(retry_after)
            raise ExchangeError("rate limited", http_status=resp.status_code)
        if resp.status_code >= 400:
            raise ExchangeError(
                f"binance REST error {resp.status_code}: {resp.text[:200]}",
                http_status=resp.status_code,
            )
        return resp

    async def ping(self) -> bool:
        try:
            await self._get("/api/v3/ping")
            return True
        except (httpx.HTTPError, ExchangeError):
            return False

    async def exchange_info(self, symbols: list[str]) -> dict[str, SymbolRules]:
        import json as _json

        resp = await self._get(
            "/api/v3/exchangeInfo", params={"symbols": _json.dumps(symbols, separators=(",", ":"))}
        )
        data = resp.json()
        for limit in data.get("rateLimits", []):
            if (limit.get("rateLimitType") == "REQUEST_WEIGHT"
                    and limit.get("interval") == "MINUTE" and int(limit.get("intervalNum", 0)) == 1):
                self.rate.limit = int(limit["limit"])
        return {s["symbol"]: parse_symbol_rules(s) for s in data.get("symbols", [])}

    async def klines(self, symbol: str, timeframe: str, *, limit: int = 1000,
                     start_ms: int | None = None, end_ms: int | None = None) -> list[Candle]:
        params: dict = {"symbol": symbol.upper(), "interval": timeframe, "limit": min(limit, 1000)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        resp = await self._get("/api/v3/klines", params=params)
        rows = resp.json()
        if not isinstance(rows, list):
            raise ExchangeError(f"unexpected klines payload: {str(rows)[:100]}")
        return [parse_rest_kline(symbol, timeframe, row) for row in rows]

    async def ticker_24h(self, symbols: list[str]) -> list[dict]:
        import json as _json

        resp = await self._get(
            "/api/v3/ticker/24hr", params={"symbols": _json.dumps(symbols, separators=(",", ":"))}
        )
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        resp = await self._get("/api/v3/depth", params={"symbol": symbol.upper(), "limit": limit})
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
