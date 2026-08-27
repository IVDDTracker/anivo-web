"""Daily / weekly report builders (markdown text → Telegram + dashboard).

Reports NEVER change system behavior automatically — they end with recommended
research areas for a human.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.models.enums import Venue
from app.storage.db import Database
from app.storage.repositories import EventRepository, OrderRepository, SignalRepository, SystemRepository


class ReportBuilder:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self.signals = SignalRepository(db)
        self.system = SystemRepository(db)
        self.events = EventRepository(db)
        self.paper = OrderRepository(db, Venue.PAPER)
        self.testnet = OrderRepository(db, Venue.TESTNET)

    async def _venue_stats(self, repo: OrderRepository, start: datetime, end: datetime) -> dict:
        positions = await repo.positions_closed_between(start, end)
        fills = await repo.fills_between(start, end)
        pnls = [float(p.realized_pnl) for p in positions]
        by_strategy: dict[str, float] = defaultdict(float)
        for p in positions:
            by_strategy[p.strategy or "?"] += float(p.realized_pnl)
        best = max(positions, key=lambda p: p.realized_pnl, default=None)
        worst = min(positions, key=lambda p: p.realized_pnl, default=None)
        return {
            "trades": len(positions),
            "net_pnl": round(sum(pnls), 2),
            "fees": round(sum(float(f.fee) for f in fills), 4),
            "wins": sum(1 for p in pnls if p > 0),
            "by_strategy": dict(by_strategy),
            "best": (f"{best.symbol} {float(best.realized_pnl):+.2f} ({best.strategy})"
                     if best else "—"),
            "worst": (f"{worst.symbol} {float(worst.realized_pnl):+.2f} ({worst.strategy})"
                      if worst else "—"),
        }

    async def daily(self, day: datetime | None = None) -> str:
        now = self.clock.now()
        end = day or now
        start = end - timedelta(days=1)
        paper = await self._venue_stats(self.paper, start, end)
        testnet = await self._venue_stats(self.testnet, start, end)
        decisions = await self.signals.recent_decisions(limit=500, since=start)
        rejected = [d for d in decisions if d["decision"] != "APPROVED"]
        reject_by_stage: dict[str, int] = defaultdict(int)
        for d in rejected:
            reject_by_stage[d.get("failed_stage") or "?"] += 1
        risk_events = [e for e in await self.system.recent_risk_events(100)
                       if datetime.fromisoformat(e["timestamp"]) >= start]
        externals = await self.events.recent_external(since=start, limit=200)
        significant = sorted((e for e in externals if e.confidence >= 0.5),
                             key=lambda e: -e.confidence)[:5]
        curve = await self.system.equity_curve("PAPER", since=end - timedelta(days=30))
        dd = curve[-1]["drawdown_pct"] if curve else 0.0

        lines = [
            f"📊 DAILY REPORT — {end.date().isoformat()}",
            "",
            f"PAPER: {paper['trades']} trades, PnL {paper['net_pnl']:+.2f}, "
            f"fees {paper['fees']:.2f}",
            f"TESTNET: {testnet['trades']} trades, PnL {testnet['net_pnl']:+.2f}",
            f"Current drawdown: {dd:.2f}%",
            f"Best trade: {paper['best']}",
            f"Worst trade: {paper['worst']}",
            "",
            f"Signals: {len(decisions)} evaluated, {len(decisions) - len(rejected)} approved, "
            f"{len(rejected)} rejected",
        ]
        if reject_by_stage:
            lines.append("Rejections by stage: " + ", ".join(
                f"{stage}: {count}" for stage, count in sorted(reject_by_stage.items())))
        lines.append(f"Risk events: {len(risk_events)}")
        if paper["by_strategy"]:
            lines.append("")
            lines.append("Strategy PnL:")
            for strategy, pnl in sorted(paper["by_strategy"].items(), key=lambda kv: -kv[1]):
                lines.append(f"• {strategy}: {pnl:+.2f}")
        if significant:
            lines.append("")
            lines.append("Major external events:")
            for e in significant:
                lines.append(f"• [{e.confidence:.2f}] {e.headline[:100]}")
        return "\n".join(lines)

    async def weekly(self) -> str:
        now = self.clock.now()
        start = now - timedelta(days=7)
        paper = await self._venue_stats(self.paper, start, now)
        decisions = await self.signals.recent_decisions(limit=2000, since=start)
        approved = [d for d in decisions if d["decision"] == "APPROVED"]
        ranking = sorted(paper["by_strategy"].items(), key=lambda kv: -kv[1])
        lines = [
            f"📈 WEEKLY REPORT — week ending {now.date().isoformat()}",
            "",
            f"PAPER: {paper['trades']} trades, net PnL {paper['net_pnl']:+.2f}, "
            f"fees {paper['fees']:.2f}, win rate "
            f"{(paper['wins'] / paper['trades'] * 100 if paper['trades'] else 0):.0f}%",
            f"Signals approved: {len(approved)} / {len(decisions)}",
            "",
            "Strategy ranking:",
        ]
        for i, (strategy, pnl) in enumerate(ranking, 1):
            lines.append(f"{i}. {strategy}: {pnl:+.2f}")
        if not ranking:
            lines.append("(no closed trades this week)")
        lines += [
            "",
            "Recommended research (manual review — nothing is changed automatically):",
            "• inspect stages with the highest rejection counts",
            "• compare live expectancy vs backtest expectancy per strategy",
            "• review parameter stability of any strategy near demotion",
        ]
        return "\n".join(lines)
