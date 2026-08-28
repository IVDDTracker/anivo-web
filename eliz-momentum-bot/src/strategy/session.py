"""TradeSession: one tweet-signal driven end-to-end through the state machine.

SIGNAL_DETECTED → MARKET_VALIDATION → ENTRY_APPROVED → LONG_OPEN → LONG_EXIT →
WAITING_SHORT_CONFIRMATION → SHORT_OPEN → SHORT_EXIT → DONE (or SKIPPED/ABORTED)

The SAME class runs in live, paper and backtest — only the execution adapter
and the tick source differ (spec §13). All exits are market-behavior driven
(reversal score / SL / TP / trailing), never a fixed timer.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.core.clock import Clock
from src.core.config import Settings
from src.core.domain import (
    AggTrade,
    BookTop,
    Classification,
    OrderIntent,
    OrderSide,
    OrderStatus,
    PositionSide,
    SkipReason,
    TweetEvent,
)
from src.core.logger import get_logger, log_ctx
from src.core.state_machine import TradeState, TradeStateMachine
from src.exchange.symbol_mapper import SymbolRules
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.risk.risk_manager import RiskManager
from src.storage.database import Repo
from src.strategy.entry import (
    EntryDecision,
    EntryInputs,
    validate_entry,
    validate_watch_entry,
)
from src.strategy.exit import LongLegManager, ShortConfirmation, ShortLegManager
from src.strategy.momentum import MomentumTracker
from src.strategy.reversal import ReversalReading, ReversalScorer

log = get_logger(__name__)


class TradeSession:
    def __init__(self, *, session_id: str, tweet: TweetEvent,
                 classification: Classification, rules: SymbolRules, cfg: Settings,
                 clock: Clock, orders: OrderManager, positions: PositionManager,
                 risk: RiskManager, repo: Repo, notifier=None) -> None:
        self.id = session_id
        self.tweet = tweet
        self.classification = classification
        self.rules = rules
        self.symbol = rules.symbol
        self.cfg = cfg
        self.clock = clock
        self.orders = orders
        self.positions = positions
        self.risk = risk
        self.repo = repo
        self.notifier = notifier

        self.sm = TradeStateMachine(session_id, on_transition=self._persist_transition)
        self.tracker: MomentumTracker | None = None
        self.scorer = ReversalScorer(cfg.reversal_weights, cfg.reversal_params)
        self.long_mgr: LongLegManager | None = None
        self.short_conf: ShortConfirmation | None = None
        self.short_mgr: ShortLegManager | None = None
        self.last_book: BookTop | None = None
        self.long_position = None
        self.short_position = None
        self.last_reversal: ReversalReading | None = None
        self.latencies: dict[str, float] = {
            "twitter_latency_ms": tweet.latency_ms,
            "classification_latency_ms": classification.classification_latency_ms,
        }

    async def _persist_transition(self, session_id: str, old: TradeState, new: TradeState,
                                  reason: str, now: datetime) -> None:
        await self.repo.store_event(session_id, old.value, new.value, reason, now)

    async def _notify(self, method: str, *args) -> None:
        if self.notifier is None:
            return
        try:
            await getattr(self.notifier, method)(*args)
        except Exception:
            log.exception("notifier.%s failed (non-fatal)", method)

    @property
    def done(self) -> bool:
        return self.sm.terminal

    # ── entry ────────────────────────────────────────────────────────────────

    async def start(self, inputs: EntryInputs) -> bool:
        now = self.clock.now()
        await self.sm.to(TradeState.MARKET_VALIDATION, "validating market conditions", now)
        decision: EntryDecision = validate_entry(
            self.tweet, self.classification, self.symbol, inputs, self.cfg)
        if decision.snapshot is not None:
            await self.repo.store_snapshot(self.id, decision.snapshot, "entry_validation")
        if not decision.approved:
            await self._skip(decision.skip_reason or SkipReason.RISK_LIMIT, decision.detail)
            return False

        stop_price = inputs.mid_price * (1.0 - self.cfg.long_params.initial_stop_pct / 100.0)
        sizing = self.risk.size_entry(price=inputs.mid_price, stop_price=stop_price,
                                      rules=self.rules, now=now)
        if not sizing.approved:
            await self._skip(sizing.skip_reason or SkipReason.RISK_LIMIT, sizing.detail)
            return False

        await self.sm.to(TradeState.ENTRY_APPROVED,
                         f"qty {sizing.quantity} ({sizing.detail})", now)
        intent = OrderIntent(
            session_id=self.id, symbol=self.symbol, side=OrderSide.BUY,
            quantity=sizing.quantity,
            order_type=self.cfg.long_params.order_type
            if self.cfg.long_params.order_type == "MARKET" else "LIMIT",
            limit_price=None, leg="LONG",
            reason=f"tweet {self.tweet.tweet_id}: {self.classification.reason[:120]}",
            created_at=now)
        result = await self.orders.submit(intent, book=self.last_book)
        total_latency = (self.clock.now() - self.tweet.created_at).total_seconds() * 1000.0
        self.latencies["binance_order_latency_ms"] = result.order_latency_ms
        self.latencies["total_signal_to_order_latency_ms"] = round(total_latency, 1)
        log_ctx(log, logging.INFO, "long entry order", session=self.id,
                status=result.status.value, **self.latencies)

        if result.status != OrderStatus.FILLED or result.executed_price is None:
            await self.sm.to(TradeState.ABORTED,
                             f"entry order {result.status}: {result.error}", self.clock.now())
            return False

        self.long_position = await self.positions.open_leg(
            session_id=self.id, tweet_id=self.tweet.tweet_id, symbol=self.symbol,
            side=PositionSide.LONG, fill=result)
        entry_price = result.executed_price
        self.tracker = MomentumTracker(
            entry_price=entry_price, entry_time=self.clock.now(),
            flow_window_s=self.cfg.reversal_params.flow_window_s,
            momentum_window_s=self.cfg.reversal_params.momentum_window_s)
        self.long_mgr = LongLegManager(
            entry_price=entry_price, entry_time=self.clock.now(),
            stop_pct=self.cfg.long_params.initial_stop_pct,
            max_holding_seconds=self.cfg.long_params.max_holding_seconds)
        await self.risk.on_trade_opened(self.clock.now())
        await self.sm.to(TradeState.LONG_OPEN,
                         f"filled {result.executed_qty} @ {entry_price}", self.clock.now())
        await self._notify("long_opened", self, result, inputs)
        return True

    async def start_watch(self, inputs: EntryInputs, *,
                          max_age_seconds: float | None = None) -> bool:
        """SHORT_ONLY entry (spec pivot): no long leg. Validate market quality,
        then MONITORING_PUMP — a real pump (≥ MIN_PUMP_PERCENT vs the message-time
        reference) followed by a reversal score breach arms the short pipeline."""
        now = self.clock.now()
        await self.sm.to(TradeState.MARKET_VALIDATION,
                         "validating market conditions (short-only watch)", now)
        decision: EntryDecision = validate_watch_entry(
            self.tweet, self.classification, self.symbol, inputs, self.cfg,
            max_age_seconds=max_age_seconds)
        if decision.snapshot is not None:
            await self.repo.store_snapshot(self.id, decision.snapshot, "watch_validation")
        if not decision.approved:
            await self._skip(decision.skip_reason or SkipReason.RISK_LIMIT, decision.detail)
            return False
        # baseline = price at message time: peak_gain then measures the PUMP itself
        self.tracker = MomentumTracker(
            entry_price=inputs.reference_price, entry_time=now,
            flow_window_s=self.cfg.reversal_params.flow_window_s,
            momentum_window_s=self.cfg.reversal_params.momentum_window_s)
        await self.sm.to(TradeState.MONITORING_PUMP, decision.detail, now)
        return True

    async def _skip(self, reason: SkipReason, detail: str) -> None:
        await self.sm.to(TradeState.SKIPPED, f"{reason}: {detail}", self.clock.now())
        await self.repo.mark_signal_skipped(self.tweet.tweet_id, reason)
        await self._notify("skipped", self, reason, detail)

    # ── market data handlers (live feed AND simulator drive these) ───────────

    async def on_book(self, book: BookTop) -> None:
        self.last_book = book
        if self.tracker is not None:
            await self.tracker.on_book(book)

    async def on_depth(self, bids: list, asks: list, ts: datetime) -> None:
        if self.tracker is not None:
            await self.tracker.on_depth(bids, asks, ts)

    async def on_trade(self, trade: AggTrade) -> None:
        if self.tracker is not None:
            await self.tracker.on_trade(trade)
        await self.evaluate(self.clock.now())

    # ── the decision loop ────────────────────────────────────────────────────

    async def evaluate(self, now: datetime) -> None:
        if self.sm.terminal or self.tracker is None:
            return
        metrics = self.tracker.metrics(now)
        reading = self.scorer.score(metrics)
        self.last_reversal = reading

        if self.sm.state == TradeState.MONITORING_PUMP:
            if metrics.seconds_since_entry > self.cfg.pump_watch_window_seconds:
                await self.sm.to(TradeState.DONE,
                                 f"watch window {self.cfg.pump_watch_window_seconds:.0f}s "
                                 f"expired (peak gain {metrics.peak_gain_pct:.2f}%)", now)
                await self._notify("session_done", self)
                return
            pumped = metrics.peak_gain_pct >= self.cfg.min_pump_percent
            if pumped and reading.score >= self.cfg.min_reversal_score:
                self.short_conf = ShortConfirmation(
                    cfg=self.cfg, long_exit_price=metrics.current_price, started_at=now)
                await self.sm.to(TradeState.WAITING_SHORT_CONFIRMATION,
                                 f"pump +{metrics.peak_gain_pct:.2f}% peaked; reversal "
                                 f"{reading.score:.0f} — awaiting confirmation", now)
            return

        if self.sm.state == TradeState.LONG_OPEN:
            protective = self.long_mgr.protective_exit(metrics.current_price, now)
            if protective is not None:
                await self._close_long(protective, metrics, reading, go_short=False)
                return
            if reading.score >= self.cfg.min_reversal_score:
                await self._close_long(f"reversal_score_{reading.score:.0f}",
                                       metrics, reading, go_short=True)
                return

        elif self.sm.state == TradeState.WAITING_SHORT_CONFIRMATION:
            verdict = self.short_conf.evaluate(reading, metrics, now)
            if verdict == "confirm":
                await self._open_short(metrics)
            elif verdict == "reject":
                await self.sm.to(TradeState.DONE,
                                 "short not confirmed (bounce/timeout) — long-only session",
                                 now)
                await self._notify("session_done", self)

        elif self.sm.state == TradeState.SHORT_OPEN:
            reason = self.short_mgr.check(metrics.current_price, now)
            if reason is not None:
                await self._close_short(reason, metrics)

    # ── leg transitions ──────────────────────────────────────────────────────

    async def _close_long(self, reason: str, metrics, reading: ReversalReading,
                          *, go_short: bool) -> None:
        now = self.clock.now()
        intent = OrderIntent(session_id=self.id, symbol=self.symbol, side=OrderSide.SELL,
                             quantity=self.long_position.qty, reduce_only=True, leg="LONG",
                             reason=f"exit: {reason}", created_at=now)
        result = await self.orders.submit(intent, book=self.last_book)
        if result.status != OrderStatus.FILLED:
            await self.sm.to(TradeState.ABORTED,
                             f"long exit order {result.status}: {result.error}", now)
            self.risk.kill.trip("ORDER_STATE_UNCERTAIN",
                                f"long exit failed on {self.symbol}")
            return
        net = await self.positions.close_leg(
            self.long_position, result, reason=reason,
            peak_price=metrics.peak_price, reversal_score=reading.score)
        await self.risk.on_leg_closed(net, float(result.fee_usdt), now)
        await self.sm.to(TradeState.LONG_EXIT, f"{reason}; net {net:+.2f} USDT", now)
        await self._notify("long_closed", self, result, net, reason)

        allow_short = go_short and not self.risk.kill.active
        if allow_short:
            self.short_conf = ShortConfirmation(
                cfg=self.cfg, long_exit_price=result.executed_price, started_at=now)
            await self.sm.to(TradeState.WAITING_SHORT_CONFIRMATION,
                             "watching for sustained reversal", now)
        else:
            await self.sm.to(TradeState.DONE, "no short leg (protective exit/kill)", now)
            await self._notify("session_done", self)

    async def _open_short(self, metrics) -> None:
        now = self.clock.now()
        stop_price = metrics.current_price * (1.0 + self.cfg.short_params.stop_loss_pct / 100.0)
        sizing = self.risk.size_entry(
            price=stop_price, stop_price=metrics.current_price, rules=self.rules, now=now)
        # NB: sizing formula expects entry>stop; for shorts risk distance is stop-entry,
        # so feed (stop, entry) — distance and caps are identical.
        if not sizing.approved:
            await self.sm.to(TradeState.DONE, f"short skipped: {sizing.detail}", now)
            await self._notify("skipped", self, sizing.skip_reason or SkipReason.RISK_LIMIT,
                               f"short leg: {sizing.detail}")
            return
        intent = OrderIntent(session_id=self.id, symbol=self.symbol, side=OrderSide.SELL,
                             quantity=sizing.quantity, leg="SHORT",
                             reason="confirmed reversal after tweet pump", created_at=now)
        result = await self.orders.submit(intent, book=self.last_book)
        if result.status != OrderStatus.FILLED or result.executed_price is None:
            await self.sm.to(TradeState.DONE,
                             f"short entry {result.status}: {result.error}", now)
            return
        self.short_position = await self.positions.open_leg(
            session_id=self.id, tweet_id=self.tweet.tweet_id, symbol=self.symbol,
            side=PositionSide.SHORT, fill=result)
        self.short_mgr = ShortLegManager(
            entry_price=result.executed_price, entry_time=now,
            params=self.cfg.short_params,
            max_holding_seconds=self.cfg.max_short_holding_seconds)
        await self.risk.on_trade_opened(now)
        await self.sm.to(TradeState.SHORT_OPEN,
                         f"short {result.executed_qty} @ {result.executed_price}", now)
        await self._notify("short_opened", self, result)

    async def _close_short(self, reason: str, metrics) -> None:
        now = self.clock.now()
        intent = OrderIntent(session_id=self.id, symbol=self.symbol, side=OrderSide.BUY,
                             quantity=self.short_position.qty, reduce_only=True,
                             leg="SHORT", reason=f"exit: {reason}", created_at=now)
        result = await self.orders.submit(intent, book=self.last_book)
        if result.status != OrderStatus.FILLED:
            await self.sm.to(TradeState.ABORTED,
                             f"short exit order {result.status}: {result.error}", now)
            self.risk.kill.trip("ORDER_STATE_UNCERTAIN",
                                f"short exit failed on {self.symbol}")
            return
        net = await self.positions.close_leg(
            self.short_position, result, reason=reason,
            reversal_score=self.last_reversal.score if self.last_reversal else None)
        await self.risk.on_leg_closed(net, float(result.fee_usdt), now)
        await self.sm.to(TradeState.SHORT_EXIT, f"{reason}; net {net:+.2f} USDT", now)
        await self.sm.to(TradeState.DONE, "session complete", now)
        await self._notify("short_closed", self, result, net, reason)
        await self._notify("session_done", self)
