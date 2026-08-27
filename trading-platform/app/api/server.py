"""FastAPI app: JSON API + local dashboard + health + Prometheus metrics.

All endpoints are read-only except the explicit operator controls
(pause/resume/risk-unlock). The server binds to 127.0.0.1 by default.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.monitoring import metrics as prom

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def create_app(platform: Any) -> FastAPI:
    """`platform` is the running Platform (app.main) or a compatible test double."""
    app = FastAPI(title="QuantLab", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.platform = platform

    # ── system ───────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> JSONResponse:
        snapshot = await platform.health.snapshot()
        ok = await platform.health.overall_ok()
        return JSONResponse(snapshot, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(prom.render(), media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/system/status")
    async def system_status() -> dict:
        return platform.status_summary()

    @app.post("/api/v1/system/pause")
    async def pause() -> dict:
        await platform.pause("api")
        return {"ok": True, "state": platform.state.state}

    @app.post("/api/v1/system/resume")
    async def resume() -> dict:
        await platform.resume("api")
        return {"ok": True, "state": platform.state.state}

    @app.post("/api/v1/system/risk-unlock")
    async def risk_unlock() -> dict:
        platform.risk.operator_unlock()
        return {"ok": True, "state": platform.state.state}

    # ── data ─────────────────────────────────────────────────────────────────

    @app.get("/api/v1/signals")
    async def signals(limit: int = Query(50, le=500)) -> list[dict]:
        return await platform.signal_repo.recent_signals(limit=limit)

    @app.get("/api/v1/decisions")
    async def decisions(limit: int = Query(50, le=500)) -> list[dict]:
        return await platform.signal_repo.recent_decisions(limit=limit)

    @app.get("/api/v1/positions")
    async def positions() -> dict:
        return await platform.positions_summary()

    @app.get("/api/v1/performance")
    async def performance(venue: str = "PAPER", days: int = Query(30, le=365)) -> dict:
        curve = await platform.system_repo.equity_curve(
            venue, since=platform.clock.now() - timedelta(days=days))
        return {"venue": venue, "equity_curve": curve, **platform.performance_summary()}

    @app.get("/api/v1/risk")
    async def risk() -> dict:
        return {
            "snapshot": platform.risk.snapshot(),
            "recent_events": await platform.system_repo.recent_risk_events(50),
        }

    @app.get("/api/v1/events")
    async def events(limit: int = Query(100, le=500)) -> list[dict]:
        items = await platform.event_repo.recent_external(
            since=platform.clock.now() - timedelta(days=7), limit=limit)
        return [e.model_dump(mode="json") for e in items]

    @app.get("/api/v1/sources")
    async def sources() -> dict:
        snapshot = await platform.health.snapshot()
        return {"collectors": snapshot["collectors"], "data_quality": snapshot["data_quality"]}

    @app.get("/api/v1/regimes")
    async def regimes() -> dict:
        return platform.regime_summary()

    @app.get("/api/v1/strategies")
    async def strategies() -> list[dict]:
        return platform.strategy_summary()

    @app.get("/api/v1/market")
    async def market() -> dict:
        return platform.market_summary()

    # ── dashboard ────────────────────────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD_PATH.read_text()

    return app
