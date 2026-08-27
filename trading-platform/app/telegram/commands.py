"""Telegram control command handlers, wired to the running Platform."""

from __future__ import annotations

from typing import Any

from app.telegram.bot import CommandRouter


def build_router(platform: Any) -> CommandRouter:
    router = CommandRouter()

    async def status(_: str) -> str:
        s = platform.status_summary()
        return ("STATUS\n"
                f"state: {s['state']} | mode: {s['execution_mode']}\n"
                f"paused: {s['paused']} | risk locked: {s['risk_locked']}\n"
                f"stale symbols: {', '.join(s['stale_symbols']) or 'none'}\n"
                f"open positions: {s['open_positions']} | equity: {s['equity']:.2f}\n"
                f"uptime: {s['uptime_s'] / 3600:.1f}h")

    async def signals(_: str) -> str:
        items = await platform.signal_repo.recent_signals(limit=5)
        if not items:
            return "No signals yet."
        lines = ["RECENT SIGNALS"]
        for s in items:
            lines.append(f"• {s['timestamp'][5:16]} {s['symbol']} {s['direction']} "
                         f"{s['strategy']} conf {s['confidence']:.0f} ({s['market_regime']})")
        return "\n".join(lines)

    async def positions(_: str) -> str:
        summary = await platform.positions_summary()
        lines = ["POSITIONS"]
        for p in summary["open"]:
            lines.append(f"• {p['venue']} {p['symbol']} qty {p['qty']} @ {p['avg_entry_price']} "
                         f"uPnL {p['unrealized_pnl']:+.2f} ({p['strategy']})")
        if len(lines) == 1:
            lines.append("none open")
        return "\n".join(lines)

    async def performance(_: str) -> str:
        p = platform.performance_summary()
        return ("PERFORMANCE (paper)\n"
                f"equity: {p['equity']:.2f}\n"
                f"unrealized: {p['unrealized_pnl']:+.2f}\n"
                f"drawdown: {p['drawdown_pct']:.2f}%\n"
                f"realized today: {p['realized_today']:+.2f}")

    async def risk(_: str) -> str:
        snap = platform.risk.snapshot()
        return ("RISK\n"
                f"locked: {snap['risk_locked']} {snap['lock_reason']}\n"
                f"PnL today: {snap['realized_pnl_today']:+.2f} | "
                f"week: {snap['realized_pnl_week']:+.2f}\n"
                f"consecutive losses: {snap['consecutive_losses']}\n"
                f"cooldown until: {snap['cooldown_until'] or '—'}")

    async def sources(_: str) -> str:
        snapshot = await platform.health.snapshot()
        lines = ["SOURCES"]
        for name, c in snapshot["collectors"].items():
            mark = "✅" if c["healthy"] else "❌"
            lines.append(f"{mark} {name}: {c['events_seen']} events, lag "
                         f"{c['lag_seconds'] if c['lag_seconds'] is not None else '∞'}s")
        return "\n".join(lines)

    async def pause(_: str) -> str:
        await platform.pause("telegram")
        return "⏸ Paused. No new positions will be opened. /resume to continue."

    async def resume(_: str) -> str:
        await platform.resume("telegram")
        return "▶️ Resumed."

    async def report(args: str) -> str:
        if args.strip().lower() == "weekly":
            return await platform.reports.weekly()
        return await platform.reports.daily()

    async def health(_: str) -> str:
        ok = await platform.health.overall_ok()
        snapshot = await platform.health.snapshot()
        return (f"HEALTH: {'OK' if ok else 'DEGRADED'}\n"
                f"db: {snapshot['database']} | redis: {snapshot['redis']}\n"
                f"queues: {sum(snapshot['queues'].values())} queued")

    for name, handler in {
        "/status": status, "/signals": signals, "/positions": positions,
        "/performance": performance, "/risk": risk, "/sources": sources,
        "/pause": pause, "/resume": resume, "/report": report, "/health": health,
    }.items():
        router.register(name, handler)
    return router
