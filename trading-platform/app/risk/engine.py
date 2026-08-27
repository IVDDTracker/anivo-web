"""Independent risk engine with absolute veto power.

Design invariants:
- Constructed independently of strategies; strategies hold no reference to it and
  cannot override or observe its state.
- Every check must AFFIRMATIVELY pass; any error/uncertainty rejects (fail-safe).
- Locks (daily/weekly loss, drawdown, consecutive losses) LATCH: they persist as
  risk events and survive restarts via `restore()`; only an operator unlock clears them.
- No martingale/averaging-down: sizing scales only with current equity & fixed risk %;
  a cooldown follows losses; SHORT entries are rejected on spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.config.settings import RiskConfig
from app.core.clock import Clock
from app.core.logging import get_logger
from app.core.state import StateMachine
from app.models.enums import Direction
from app.models.market import SymbolRules
from app.models.orders import Position, RiskDecision
from app.portfolio.correlation import CorrelationEngine
from app.portfolio.sizing import apply_caps, fixed_fractional_qty

log = get_logger(__name__)


@dataclass
class EntryRequest:
    """Everything the risk engine needs to judge a proposed entry."""

    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float | None
    signal_confidence: float          # 0-100 (fused score)
    data_quality: float               # 0-1
    spread_pct: float | None          # e.g. 0.05 = 5 bps... expressed in percent
    quote_volume_24h: float | None
    equity: float
    open_positions: list[Position]
    mark_prices: dict[str, float] = field(default_factory=dict)
    rules: SymbolRules | None = None


@dataclass
class RiskEngine:
    cfg: RiskConfig
    clock: Clock
    state: StateMachine
    correlations: CorrelationEngine | None = None

    # rolling accounting (restored from persistence at startup)
    realized_pnl_today: float = 0.0
    realized_pnl_week: float = 0.0
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    _current_day: datetime | None = None
    _current_week: int | None = None
    events: list[dict] = field(default_factory=list)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def restore(self, *, peak_equity: float, realized_today: float, realized_week: float,
                consecutive_losses: int, locked_reason: str | None) -> None:
        self.peak_equity = peak_equity
        self.realized_pnl_today = realized_today
        self.realized_pnl_week = realized_week
        self.consecutive_losses = consecutive_losses
        if locked_reason:
            self.state.risk_lock(locked_reason)

    def _roll_periods(self, now: datetime) -> None:
        day = now.date()
        if self._current_day is not None and day != self._current_day:
            self.realized_pnl_today = 0.0
        self._current_day = day
        week = now.isocalendar()[1]
        if self._current_week is not None and week != self._current_week:
            self.realized_pnl_week = 0.0
        self._current_week = week

    def _record(self, kind: str, detail: str, symbol: str | None = None) -> None:
        self.events.append({"ts": self.clock.now(), "kind": kind, "detail": detail,
                            "symbol": symbol})
        log.warning("risk event %s: %s", kind, detail)

    # ── feedback from execution ──────────────────────────────────────────────

    def on_position_closed(self, pnl: float, equity: float) -> None:
        now = self.clock.now()
        self._roll_periods(now)
        self.realized_pnl_today += pnl
        self.realized_pnl_week += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            self.cooldown_until = now + timedelta(minutes=self.cfg.cooldown_after_loss_minutes)
            self._record("cooldown", f"loss of {pnl:.2f}; cooldown until {self.cooldown_until}")
        else:
            self.consecutive_losses = 0
        self._check_locks(equity)

    def on_volatility_shock(self, symbol: str, move_pct: float) -> None:
        if abs(move_pct) >= self.cfg.vol_shock_return_pct:
            self.cooldown_until = self.clock.now() + timedelta(
                minutes=self.cfg.cooldown_after_vol_shock_minutes)
            self._record("vol_shock_cooldown",
                         f"{symbol} moved {move_pct:.2f}% in 1m; cooldown until {self.cooldown_until}",
                         symbol)

    def update_equity(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        self._check_locks(equity)

    def _check_locks(self, equity: float) -> None:
        if equity <= 0:
            self.state.risk_lock("equity non-positive")
            self._record("risk_lock", "equity non-positive")
            return
        if self.peak_equity > 0:
            dd_pct = (self.peak_equity - equity) / self.peak_equity * 100.0
            if dd_pct >= self.cfg.max_drawdown_pct:
                self.state.risk_lock(f"max drawdown {dd_pct:.1f}% ≥ {self.cfg.max_drawdown_pct}%")
                self._record("risk_lock", f"drawdown lock at {dd_pct:.1f}%")
                return
        daily_loss_pct = -self.realized_pnl_today / equity * 100.0 if equity > 0 else 100.0
        if daily_loss_pct >= self.cfg.max_daily_loss_pct:
            self.state.risk_lock(
                f"daily loss {daily_loss_pct:.2f}% ≥ {self.cfg.max_daily_loss_pct}%")
            self._record("risk_lock", f"daily loss lock at {daily_loss_pct:.2f}%")
            return
        weekly_loss_pct = -self.realized_pnl_week / equity * 100.0 if equity > 0 else 100.0
        if weekly_loss_pct >= self.cfg.max_weekly_loss_pct:
            self.state.risk_lock(
                f"weekly loss {weekly_loss_pct:.2f}% ≥ {self.cfg.max_weekly_loss_pct}%")
            self._record("risk_lock", f"weekly loss lock at {weekly_loss_pct:.2f}%")

    def operator_unlock(self) -> None:
        self.state.risk_unlock()
        self._record("risk_unlock", "operator cleared risk lock")

    # ── the veto ─────────────────────────────────────────────────────────────

    def evaluate_entry(self, req: EntryRequest) -> RiskDecision:
        """Absolute veto. Returns approved sizing only if EVERY check passes."""
        now = self.clock.now()
        self._roll_periods(now)
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def fail(name: str, reason: str) -> None:
            checks[name] = False
            reasons.append(reason)

        def ok(name: str) -> None:
            checks[name] = True

        # 0. system gates (pause / risk lock / staleness / degraded)
        allowed, gate_reason = self.state.can_open_new_positions(req.symbol)
        if allowed:
            ok("system_state")
        else:
            fail("system_state", gate_reason)

        # 1. spot-only: no shorts, no leverage
        if req.direction != Direction.LONG:
            fail("spot_long_only", f"{req.direction} entries not allowed on spot (no margin)")
        else:
            ok("spot_long_only")

        # 2. cooldowns
        if self.cooldown_until is not None and now < self.cooldown_until:
            fail("cooldown", f"cooldown active until {self.cooldown_until.isoformat()}")
        else:
            ok("cooldown")

        # 3. consecutive losses
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            fail("consecutive_losses",
                 f"{self.consecutive_losses} consecutive losses ≥ {self.cfg.max_consecutive_losses}")
        else:
            ok("consecutive_losses")

        # 4. signal quality
        if req.signal_confidence < self.cfg.min_signal_confidence:
            fail("min_confidence",
                 f"confidence {req.signal_confidence:.0f} < {self.cfg.min_signal_confidence:.0f}")
        else:
            ok("min_confidence")
        if req.data_quality < self.cfg.min_data_quality:
            fail("data_quality", f"data quality {req.data_quality:.2f} < {self.cfg.min_data_quality}")
        else:
            ok("data_quality")

        # 5. market quality
        if req.spread_pct is None:
            fail("spread", "spread unknown (no book ticker)")
        elif req.spread_pct > self.cfg.max_spread_pct:
            fail("spread", f"spread {req.spread_pct:.3f}% > {self.cfg.max_spread_pct}%")
        else:
            ok("spread")
        if req.quote_volume_24h is None:
            fail("liquidity", "24h volume unknown")
        elif req.quote_volume_24h < self.cfg.min_liquidity_quote_vol_24h:
            fail("liquidity",
                 f"24h quote vol {req.quote_volume_24h:.0f} < {self.cfg.min_liquidity_quote_vol_24h:.0f}")
        else:
            ok("liquidity")

        # 6. price sanity
        if req.entry_price <= 0 or (req.stop_price is not None and req.stop_price <= 0):
            fail("price_sanity", "non-positive price")
        elif req.stop_price is not None and req.stop_price >= req.entry_price:
            fail("price_sanity", "stop above entry for a LONG")
        else:
            ok("price_sanity")

        # 7. position count
        open_count = len([p for p in req.open_positions if p.is_open])
        if open_count >= self.cfg.max_open_positions:
            fail("max_positions", f"{open_count} open ≥ {self.cfg.max_open_positions}")
        else:
            ok("max_positions")

        # 8. per-asset exposure
        open_notional: dict[str, float] = {}
        for p in req.open_positions:
            if p.is_open:
                mark = req.mark_prices.get(p.symbol, float(p.avg_entry_price))
                open_notional[p.symbol] = open_notional.get(p.symbol, 0.0) + float(p.qty) * mark
        asset_notional = open_notional.get(req.symbol, 0.0)
        max_asset_notional = req.equity * self.cfg.max_exposure_per_asset_pct / 100.0
        if asset_notional >= max_asset_notional:
            fail("asset_exposure",
                 f"{req.symbol} exposure {asset_notional:.0f} ≥ {max_asset_notional:.0f}")
        else:
            ok("asset_exposure")

        # 9. correlated exposure — fail-safe: unknown correlation counts as correlated
        if self.correlations is not None:
            corr_notional = self.correlations.correlated_notional(
                req.symbol, open_notional, threshold=self.cfg.correlation_threshold)
        else:
            corr_notional = sum(abs(v) for v in open_notional.values())
        max_corr = req.equity * self.cfg.max_correlated_exposure_pct / 100.0
        if corr_notional >= max_corr:
            fail("correlated_exposure",
                 f"correlated exposure {corr_notional:.0f} ≥ {max_corr:.0f}")
        else:
            ok("correlated_exposure")

        if not all(checks.values()):
            self._record("entry_rejected", "; ".join(reasons), req.symbol)
            return RiskDecision(approved=False, reasons=reasons, checks=checks)

        # sizing (risk-based, capped, filter-compliant)
        if req.stop_price is not None:
            qty = fixed_fractional_qty(
                equity=req.equity, risk_pct=self.cfg.max_risk_per_position_pct,
                entry=req.entry_price, stop=req.stop_price)
        else:
            qty = (req.equity * self.cfg.max_position_notional_pct / 100.0) / req.entry_price
        original = qty
        qty = apply_caps(qty, entry=req.entry_price, equity=req.equity,
                         max_notional_pct=self.cfg.max_position_notional_pct, rules=req.rules)
        # remaining per-asset headroom
        headroom = max_asset_notional - asset_notional
        if qty * req.entry_price > headroom:
            qty = headroom / req.entry_price
            qty = apply_caps(qty, entry=req.entry_price, equity=req.equity,
                             max_notional_pct=self.cfg.max_position_notional_pct, rules=req.rules)
        if qty <= 0:
            reason = "sized to zero (caps/filters)"
            self._record("entry_rejected", reason, req.symbol)
            return RiskDecision(approved=False, reasons=[reason], checks={**checks, "sizing": False})

        checks["sizing"] = True
        return RiskDecision(
            approved=True, reasons=["all checks passed"], checks=checks,
            original_quantity=Decimal(str(original)), approved_quantity=Decimal(str(qty)),
        )

    def snapshot(self) -> dict:
        return {
            "realized_pnl_today": round(self.realized_pnl_today, 2),
            "realized_pnl_week": round(self.realized_pnl_week, 2),
            "peak_equity": round(self.peak_equity, 2),
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "risk_locked": self.state.status().risk_locked,
            "lock_reason": self.state.status().reason,
            "limits": self.cfg.model_dump(),
        }
