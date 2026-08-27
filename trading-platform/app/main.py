"""QuantLab application entry point: builds and supervises the whole platform.

Run: python -m app.main   (Postgres + Redis must be reachable; see docker-compose.yml)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal as os_signal
from datetime import datetime, timedelta
from decimal import Decimal

from app.api.server import create_app
from app.config.settings import Settings, get_settings
from app.config.sources import load_github_sources, load_rss_sources, load_telegram_sources
from app.core.bus import EventBus, Topics
from app.core.clock import RealClock, utcnow
from app.core.logging import get_logger, register_secret, setup_logging
from app.core.state import StateMachine
from app.core.supervisor import Supervisor
from app.data.collectors.binance_rest import BinanceMarketData
from app.data.collectors.binance_ws import BinanceWsCollector, backfill_history
from app.data.collectors.github import GitHubCollector
from app.data.collectors.rss import ExternalPoller, FearGreedProvider, RssProvider
from app.data.intelligence import EventIntelligence
from app.data.quality import DataQualityService
from app.features.engine import FeatureEngine
from app.features.microstructure import MicrostructureTracker
from app.models.enums import (
    Direction,
    ExecutionMode,
    SignalDecision,
    StrategyStage,
    Venue,
)
from app.monitoring import metrics as prom
from app.monitoring.degradation import DegradationDetector
from app.monitoring.health import HealthService
from app.paper.engine import PaperEngine
from app.pipeline import DecisionPipeline, PipelineDeps
from app.portfolio.correlation import CorrelationEngine
from app.regimes.classifier import classify
from app.reports.builder import ReportBuilder
from app.risk.engine import RiskEngine
from app.storage.db import Database
from app.storage.redis_client import HotState
from app.storage.repositories import (
    CandleRepository,
    EventRepository,
    OrderRepository,
    RetentionService,
    SignalRepository,
    SystemRepository,
)
from app.strategies.base import OpenPositionView, StrategyContext
from app.strategies.registry import StrategyRegistry
from app.telegram.bot import TelegramBot
from app.telegram.client import TelegramClient
from app.telegram.commands import build_router
from app.telegram.ingest import TelegramIngest
from app.telegram.notifier import Notifier

log = get_logger(__name__)


class Platform:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.clock = RealClock()
        self.started_at = utcnow()
        self.bus = EventBus()
        self.state = StateMachine(clock=self.clock, execution_mode=self.settings.execution_mode)
        self.supervisor = Supervisor(state=self.state)

        self.db = Database(self.settings.database_url)
        self.hot = HotState(self.settings.redis_url)
        self.event_repo = EventRepository(self.db)
        self.candle_repo = CandleRepository(self.db)
        self.signal_repo = SignalRepository(self.db)
        self.system_repo = SystemRepository(self.db)
        self.paper_repo = OrderRepository(self.db, Venue.PAPER)
        self.testnet_repo = OrderRepository(self.db, Venue.TESTNET)

        self.quality = DataQualityService(
            clock=self.clock, state=self.state,
            max_age_s=self.settings.risk.stale_data_max_age_s,
            abnormal_jump_pct=self.settings.risk.abnormal_price_jump_pct)
        self.micro = MicrostructureTracker()
        self.features = FeatureEngine()
        self.correlations = CorrelationEngine()
        self.risk = RiskEngine(cfg=self.settings.risk, clock=self.clock, state=self.state,
                               correlations=self.correlations)
        self.intelligence = EventIntelligence(self.event_repo, self.clock, self.bus)
        self.registry = StrategyRegistry(clock=self.clock, db=self.db)
        self.registry.register_baselines(initial_stage=StrategyStage.PAPER)

        self.rest = BinanceMarketData(
            self.settings.binance.market_rest_base,
            timeout_s=self.settings.binance.request_timeout_s,
            fallback_weight_per_minute=self.settings.binance.fallback_weight_per_minute)
        self.ws = BinanceWsCollector(
            ws_base=self.settings.binance.market_ws_base,
            symbols=self.settings.all_symbols, timeframes=self.settings.timeframes,
            bus=self.bus, events=self.event_repo, candles=self.candle_repo, rest=self.rest,
            hot=self.hot, reconnect_before_h=self.settings.binance.ws_reconnect_before_h,
            stale_after_s=self.settings.binance.ws_stale_after_s)

        self.paper = PaperEngine(
            costs=self.settings.costs, clock=self.clock, repo=self.paper_repo, bus=self.bus,
            starting_cash=Decimal(str(self.settings.risk.starting_equity_usdt)),
            on_position_closed=self._on_position_closed)

        # telegram (optional)
        self.notifier: Notifier
        self.telegram_bot: TelegramBot | None = None
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            tg_client = TelegramClient(
                self.settings.telegram_bot_token, api_base=self.settings.telegram.api_base,
                min_send_interval_s=self.settings.telegram.min_notify_interval_s)
            self.notifier = Notifier(client=tg_client, chat_id=self.settings.telegram_chat_id,
                                     clock=self.clock)
            ingest = TelegramIngest(
                load_telegram_sources(self.settings.config_dir), self.bus,
                reliability_cap=self.settings.telegram.ingest_reliability_cap)
            self.telegram_bot = TelegramBot(
                tg_client, admin_chat_id=self.settings.telegram_chat_id,
                router=build_router(self), ingest_handler=ingest.handle_message,
                ingest_chat_ids=ingest.chat_ids,
                poll_timeout_s=self.settings.telegram.poll_timeout_s)
        else:
            self.notifier = Notifier(client=None, chat_id="", clock=self.clock)

        # external collectors
        self.github = GitHubCollector(
            load_github_sources(self.settings.config_dir), self.bus,
            token=self.settings.github_token, api_base=self.settings.github.api_base,
            api_version=self.settings.github.api_version,
            poll_interval_s=self.settings.github.poll_interval_s)
        providers = [RssProvider(cfg) for cfg in load_rss_sources(self.settings.config_dir)
                     if cfg.enabled]
        providers.append(FearGreedProvider())
        self.external = ExternalPoller(providers, self.bus)

        # testnet execution (optional)
        self.testnet = None
        self.reconciler = None
        if (self.settings.execution_mode == ExecutionMode.TESTNET_ACTIVE
                and self.settings.binance_testnet_api_key):
            from app.execution.binance_client import BinanceSignedClient
            from app.execution.testnet import TestnetExecutor, TestnetReconciler

            signed = BinanceSignedClient(
                self.settings.binance.testnet_rest_base,
                self.settings.binance_testnet_api_key,
                self.settings.binance_testnet_api_secret,
                recv_window_ms=self.settings.binance.recv_window_ms)
            self.rules: dict = {}
            self.testnet = TestnetExecutor(signed, self.testnet_repo, self.clock,
                                           rules=self.rules, bus=self.bus)
            self.reconciler = TestnetReconciler(self.testnet, self.testnet_repo, self.clock,
                                                state=self.state)
        else:
            self.rules = {}

        self.ticker_volumes: dict[str, float] = {}
        self.mark_prices: dict[str, float] = {}
        self.regimes: dict[str, dict] = {}
        self.pipeline = DecisionPipeline(PipelineDeps(
            settings=self.settings, clock=self.clock, state=self.state, quality=self.quality,
            risk=self.risk, micro=self.micro, intelligence=self.intelligence, rules=self.rules,
            ticker_24h_quote_volume=self.ticker_volumes,
            open_positions_provider=self._open_positions,
            equity_provider=lambda: float(self.paper.equity()),
            mark_prices=self.mark_prices, strategy_stages=self.registry.stages()))
        self.degradation = DegradationDetector(registry=self.registry, clock=self.clock,
                                               notifier=self.notifier)
        self.reports = ReportBuilder(self.db, self.clock)
        self.health = HealthService(
            clock=self.clock, state=self.state, db=self.db, hot=self.hot, bus=self.bus,
            quality=self.quality,
            collectors=[c for c in (self.ws, self.github, self.external, self.telegram_bot)
                        if c is not None])
        self._last_depth_persist: dict[str, datetime] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        setup_logging(self.settings.log_level)
        for secret in self.settings.secrets():
            register_secret(secret)
        log.info("starting QuantLab (mode=%s, symbols=%s)",
                 self.settings.execution_mode, self.settings.symbols)
        await self.db.create_all()  # no-op when Alembic already migrated
        if not await self.hot.healthcheck():
            log.warning("redis unreachable at startup — kill switch fails safe (blocks entries)")

        # exchange rules + 24h volumes (tolerate failure: entries stay blocked until known)
        await self._refresh_exchange_info()
        await self._refresh_ticker_volumes()

        # market history: backfill + warm features/correlations
        try:
            count = await backfill_history(self.rest, self.candle_repo,
                                           self.settings.all_symbols,
                                           self.settings.timeframes, limit=1000)
            log.info("backfilled %d candles", count)
        except Exception:
            log.exception("startup backfill failed; features warm from DB only")
        for symbol in self.settings.all_symbols:
            for tf in self.settings.timeframes:
                candles = await self.candle_repo.fetch(symbol, tf, limit=1200)
                if candles:
                    self.features.warm(symbol, tf, candles)
            history = self.features.history(symbol, self.settings.signal_timeframe)
            if history:
                self.correlations.update(symbol, history)

        # restore state
        await self.paper.restore()
        await self.registry.restore()
        self.risk.update_equity(float(self.paper.equity()))
        if self.reconciler is not None:
            await self.reconciler.reconcile_once()

        # consumers
        self.supervisor.start("binance_ws", self.ws.run)
        self.supervisor.start("consume_tickers", self._consume_tickers, mark_degraded=False)
        self.supervisor.start("consume_trades", self._consume_trades, mark_degraded=False)
        self.supervisor.start("consume_depth", self._consume_depth, mark_degraded=False)
        self.supervisor.start("consume_candles", self._consume_candles, mark_degraded=False)
        self.supervisor.start("consume_external", self._consume_external, mark_degraded=False)
        self.supervisor.start("consume_notify", self._consume_notify, mark_degraded=False)
        self.supervisor.start("consume_positions", self._consume_positions, mark_degraded=False)
        self.supervisor.start("github", self.github.run, mark_degraded=False)
        self.supervisor.start("external_rss", self.external.run, mark_degraded=False)
        if self.telegram_bot is not None:
            self.supervisor.start("telegram_bot", self.telegram_bot.run, mark_degraded=False)
            self.supervisor.start("notifier", self.notifier.run, mark_degraded=False)
        self.supervisor.start("freshness", self._freshness_loop, mark_degraded=False)
        self.supervisor.start("ticker24h", self._ticker24h_loop, mark_degraded=False)
        self.supervisor.start("snapshots", self._snapshot_loop, mark_degraded=False)
        self.supervisor.start("retention", self._retention_loop, mark_degraded=False)
        self.supervisor.start("reporting", self._report_loop, mark_degraded=False)
        self.supervisor.start("kill_switch", self._kill_switch_loop, mark_degraded=False)
        if self.reconciler is not None:
            self.supervisor.start("testnet_reconcile", self._reconcile_loop, mark_degraded=False)

        self.state.mark_started()
        await self.system_repo.system_event(self.clock.now(), "startup",
                                            f"mode={self.settings.execution_mode}")
        self.notifier.system_alert("startup", f"QuantLab started ({self.settings.execution_mode})")

    async def stop(self) -> None:
        await self.supervisor.stop()
        await self.rest.close()
        await self.hot.close()
        await self.db.dispose()

    async def run(self) -> None:
        await self.start()
        import uvicorn

        config = uvicorn.Config(create_app(self), host=self.settings.api_host,
                                port=self.settings.api_port, log_level="warning")
        server = uvicorn.Server(config)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        api_task = asyncio.create_task(server.serve())
        await stop_event.wait()
        log.info("shutdown requested")
        server.should_exit = True
        await api_task
        await self.stop()

    # ── startup helpers ──────────────────────────────────────────────────────

    async def _refresh_exchange_info(self) -> None:
        try:
            rules = await self.rest.exchange_info(self.settings.all_symbols)
            self.rules.update(rules)
            log.info("loaded exchange rules for %s", sorted(rules))
        except Exception:
            log.exception("exchangeInfo fetch failed — filter-dependent paths stay disabled")

    async def _refresh_ticker_volumes(self) -> None:
        try:
            for t in await self.rest.ticker_24h(self.settings.all_symbols):
                self.ticker_volumes[t["symbol"]] = float(t.get("quoteVolume", 0.0))
        except Exception:
            log.exception("24h ticker fetch failed — liquidity gate fails safe")

    # ── bus consumers ────────────────────────────────────────────────────────

    async def _consume_tickers(self) -> None:
        sub = self.bus.subscribe(Topics.BOOK_TICKER, maxsize=2000, drop_oldest=True,
                                 name="platform")
        async for ticker in self.bus.iter(sub):
            accepted = self.quality.on_price(ticker.symbol, ticker.mid, ticker.timestamp)
            self.micro.on_book_ticker(ticker)
            if accepted:
                self.mark_prices[ticker.symbol] = ticker.mid
                await self.paper.on_book_ticker(ticker)

    async def _consume_trades(self) -> None:
        sub = self.bus.subscribe(Topics.TRADE, maxsize=5000, drop_oldest=True, name="platform")
        async for tick in self.bus.iter(sub):
            self.micro.on_trade(tick)

    async def _consume_depth(self) -> None:
        from app.storage.tables import OrderbookSnapshotRow

        sub = self.bus.subscribe(Topics.DEPTH, maxsize=500, drop_oldest=True, name="platform")
        async for snap in self.bus.iter(sub):
            self.micro.on_depth(snap)
            last = self._last_depth_persist.get(snap.symbol)
            if last is None or (snap.timestamp - last) >= timedelta(seconds=60):
                self._last_depth_persist[snap.symbol] = snap.timestamp
                try:
                    async with self.db.session() as s:
                        s.add(OrderbookSnapshotRow(
                            symbol=snap.symbol, timestamp=snap.timestamp,
                            bids=[[level.price, level.qty] for level in snap.bids],
                            asks=[[level.price, level.qty] for level in snap.asks]))
                except Exception:
                    log.exception("depth snapshot persist failed")

    async def _consume_candles(self) -> None:
        sub = self.bus.subscribe(Topics.CANDLE_CLOSED, maxsize=2000, name="platform")
        async for candle in self.bus.iter(sub):
            try:
                await self._on_candle(candle)
            except Exception:
                log.exception("candle handling failed for %s %s", candle.symbol,
                              candle.timeframe)

    async def _consume_external(self) -> None:
        sub = self.bus.subscribe(Topics.EXTERNAL_EVENT, maxsize=2000, name="platform")
        async for event in self.bus.iter(sub):
            try:
                await self.intelligence.process(event)
            except Exception:
                log.exception("external event processing failed")

    async def _consume_notify(self) -> None:
        sub = self.bus.subscribe(Topics.NOTIFY, maxsize=500, name="platform")
        async for item in self.bus.iter(sub):
            if isinstance(item, dict) and item.get("kind") == "external_event":
                self.notifier.external_event(item["headline"], item["source"],
                                             item["confidence"], item.get("assets", []))

    async def _consume_positions(self) -> None:
        sub = self.bus.subscribe(Topics.POSITION_UPDATE, maxsize=500, name="platform")
        async for pos in self.bus.iter(sub):
            if pos.closed_at is not None:
                self.notifier.position_closed(pos)
            elif pos.is_open:
                self.notifier.position_opened(pos)

    # ── candle-driven trading loop ───────────────────────────────────────────

    async def _on_candle(self, candle) -> None:
        feats = self.features.on_candle(candle)
        if candle.timeframe == "1m":
            move_pct = (candle.close / candle.open - 1.0) * 100.0 if candle.open > 0 else 0.0
            if abs(move_pct) >= self.settings.risk.vol_shock_return_pct:
                self.risk.on_volatility_shock(candle.symbol, move_pct)
        if (candle.timeframe != self.settings.signal_timeframe
                or candle.symbol not in self.settings.symbols):
            return
        history = self.features.history(candle.symbol, candle.timeframe)
        self.correlations.update(candle.symbol, history)
        regime = classify(history, feats, self.settings.regime,
                          quote_volume_24h=self.ticker_volumes.get(candle.symbol),
                          min_liquidity=self.settings.risk.min_liquidity_quote_vol_24h)
        await self._track_regime(candle.symbol, candle.timeframe, regime)
        evidence = 0.0
        try:
            evidence = await self.intelligence.evidence_score(candle.symbol)
        except Exception:
            log.exception("evidence lookup failed")
        ctx = StrategyContext(
            symbol=candle.symbol, timeframe=candle.timeframe, now=self.clock.now(),
            candles=history, features=feats, regime=regime,
            micro=self.micro.features(candle.symbol, self.clock.now()),
            event_evidence=evidence,
            data_quality=self.quality.quality_score(candle.symbol))

        open_position = self.paper.positions.get(candle.symbol)
        for record in self.registry.active():
            strategy = record.instance
            if open_position is not None and open_position.strategy == strategy.name:
                reason = strategy.should_exit(ctx, OpenPositionView(
                    direction=Direction.LONG,
                    entry_price=float(open_position.avg_entry_price),
                    entry_time=open_position.opened_at,
                    stop=float(open_position.stop_price) if open_position.stop_price else None,
                    target=(float(open_position.target_price)
                            if open_position.target_price else None)))
                if reason:
                    await self.paper.close_position(candle.symbol, reason=reason)
                    open_position = None
                continue  # a strategy holding a position doesn't stack entries
            signal = strategy.generate_signal(ctx)
            if signal is None:
                continue
            prom.SIGNALS_GENERATED.labels(strategy=strategy.name, symbol=candle.symbol).inc()
            await self.signal_repo.store_signal(signal)
            decision_record, intent = await self.pipeline.decide(signal, strategy)
            await self.signal_repo.store_decision(decision_record)
            if decision_record.decision != SignalDecision.APPROVED:
                failed = decision_record.failed_stage()
                prom.SIGNALS_REJECTED.labels(stage=failed.value if failed else "?").inc()
                continue
            status_line = "REJECTED"
            if intent is not None and intent.venue == Venue.TESTNET and self.testnet:
                order = await self.testnet.submit(intent)
                status_line = f"TESTNET ORDER {order.status}"
            elif intent is not None:
                await self.paper.submit(intent)
                status_line = "PAPER TRADE OPENED"
            self.notifier.signal_detected(signal, decision_record, status_line)

    async def _track_regime(self, symbol: str, timeframe: str, regime) -> None:
        previous = self.regimes.get(symbol)
        current = {"regime": regime.regime.value, "volatility": regime.volatility_state,
                   "rules": regime.rules_fired,
                   "timestamp": self.clock.now().isoformat()}
        if previous is None or previous["regime"] != current["regime"]:
            await self.system_repo.regime_change(
                self.clock.now(), symbol, timeframe, regime.regime.value,
                regime.volatility_state, {"rules": regime.rules_fired, **regime.metrics})
            if previous is not None:
                self.notifier.regime_change(symbol, previous["regime"], current["regime"])
        self.regimes[symbol] = current

    async def _on_position_closed(self, position, pnl: float) -> None:
        equity = float(self.paper.equity())
        self.risk.on_position_closed(pnl, equity)
        await self.degradation.on_position_closed(position)
        for event in self.risk.events[-3:]:
            if event["kind"] == "risk_lock":
                self.notifier.risk_alert("risk_lock", event["detail"])
        await self.system_repo.risk_event(self.clock.now(), "position_closed",
                                          f"{position.symbol} pnl {pnl:.2f}",
                                          symbol=position.symbol)

    # ── periodic loops ───────────────────────────────────────────────────────

    async def _freshness_loop(self) -> None:
        while True:
            stale = self.quality.check_freshness()
            for symbol, sq in self.quality.snapshot().items():
                if sq["age_s"] is not None:
                    prom.DATA_LAG.labels(symbol=symbol).set(sq["age_s"])
            for symbol, is_stale in stale.items():
                if is_stale:
                    self.notifier.source_unhealthy(f"binance:{symbol}",
                                                   "market data stale — no new positions")
            for queue, size in self.bus.queue_sizes().items():
                prom.QUEUE_SIZE.labels(queue=queue).set(size)
            await asyncio.sleep(10)

    async def _ticker24h_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            await self._refresh_ticker_volumes()

    async def _snapshot_loop(self) -> None:
        while True:
            try:
                equity = self.paper.equity()
                self.risk.update_equity(float(equity))
                await self.system_repo.performance_snapshot(
                    self.clock.now(), "PAPER", equity, self.paper.cash,
                    self.paper.unrealized_pnl(),
                    Decimal(str(self.risk.realized_pnl_today)),
                    self.paper.drawdown_pct(), len(self.paper.positions))
                prom.EQUITY.labels(venue="PAPER").set(float(equity))
                prom.DRAWDOWN.labels(venue="PAPER").set(self.paper.drawdown_pct())
                prom.OPEN_POSITIONS.labels(venue="PAPER").set(len(self.paper.positions))
            except Exception:
                log.exception("snapshot loop failed")
            await asyncio.sleep(300)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reconciler.reconcile_once()

    async def _retention_loop(self) -> None:
        retention = RetentionService(
            self.db, raw_hf_days=self.settings.retention.raw_hf_events_days,
            candles_1m_days=self.settings.retention.candles_1m_days,
            orderbook_days=self.settings.retention.orderbook_snapshots_days,
            features_days=self.settings.retention.features_days)
        while True:
            await asyncio.sleep(24 * 3600)
            deleted = await retention.run_once(self.clock.now())
            log.info("retention deleted: %s", deleted)

    async def _report_loop(self) -> None:
        last_daily: str | None = None
        while True:
            now = self.clock.now()
            key = now.date().isoformat()
            if now.hour >= 0 and last_daily != key and now.hour == 0 and now.minute >= 5:
                try:
                    self.notifier.report(await self.reports.daily())
                    if now.weekday() == 0:
                        self.notifier.report(await self.reports.weekly())
                    last_daily = key
                except Exception:
                    log.exception("report generation failed")
            await asyncio.sleep(60)

    async def _kill_switch_loop(self) -> None:
        while True:
            if await self.hot.kill_switch_on() and not self.state.status().paused:
                log.warning("kill switch flag detected — pausing")
                await self.pause("kill-switch")
            await asyncio.sleep(10)

    # ── operator controls ────────────────────────────────────────────────────

    async def pause(self, source: str) -> None:
        self.state.pause()
        await self.system_repo.system_event(self.clock.now(), "pause", f"by {source}")
        self.notifier.system_alert("paused", f"trading paused (by {source})")

    async def resume(self, source: str) -> None:
        try:
            await self.hot.set_kill_switch(False)
        except Exception:
            # redis down: resume locally anyway — the kill-switch loop fails safe
            # and will re-pause if the flag is actually set once redis returns
            log.exception("could not clear kill switch flag (redis down?)")
        self.state.resume()
        await self.system_repo.system_event(self.clock.now(), "resume", f"by {source}")
        self.notifier.system_alert("resumed", f"trading resumed (by {source})")

    # ── API summaries ────────────────────────────────────────────────────────

    async def _open_positions(self):
        paper = list(self.paper.positions.values())
        testnet = await self.testnet_repo.open_positions() if self.testnet else []
        return paper + testnet

    def status_summary(self) -> dict:
        status = self.state.status()
        return {
            "state": status.state.value, "execution_mode": status.execution_mode.value,
            "paused": status.paused, "risk_locked": status.risk_locked,
            "stale_symbols": status.stale_symbols, "reason": status.reason,
            "open_positions": len(self.paper.positions),
            "equity": float(self.paper.equity()),
            "uptime_s": (utcnow() - self.started_at).total_seconds(),
        }

    async def positions_summary(self) -> dict:
        open_out = []
        for pos in await self._open_positions():
            mark = self.mark_prices.get(pos.symbol, float(pos.avg_entry_price))
            open_out.append({
                "venue": pos.venue.value, "symbol": pos.symbol, "qty": float(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "stop_price": float(pos.stop_price) if pos.stop_price else None,
                "target_price": float(pos.target_price) if pos.target_price else None,
                "unrealized_pnl": float(pos.unrealized_pnl(Decimal(str(mark)))),
                "strategy": pos.strategy, "opened_at": pos.opened_at.isoformat(),
            })
        closed_out = []
        since = self.clock.now() - timedelta(days=2)
        for repo in (self.paper_repo, self.testnet_repo):
            for pos in await repo.positions_closed_between(since, self.clock.now()):
                closed_out.append({
                    "venue": pos.venue.value, "symbol": pos.symbol, "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "realized_pnl": float(pos.realized_pnl),
                    "fees_paid": float(pos.fees_paid), "close_reason": pos.close_reason,
                    "strategy": pos.strategy,
                    "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
                })
        closed_out.sort(key=lambda p: p["closed_at"] or "", reverse=True)
        return {"open": open_out, "recently_closed": closed_out[:50]}

    def performance_summary(self) -> dict:
        return {
            "equity": float(self.paper.equity()),
            "cash": float(self.paper.cash),
            "unrealized_pnl": float(self.paper.unrealized_pnl()),
            "drawdown_pct": self.paper.drawdown_pct(),
            "realized_today": self.risk.realized_pnl_today,
        }

    def regime_summary(self) -> dict:
        return {"regimes": self.regimes}

    def strategy_summary(self) -> list[dict]:
        live = self.degradation.snapshot()
        return [
            {"name": name, "version": r.instance.version, "stage": r.stage.value,
             "enabled": r.enabled, "scorecard": r.scorecard,
             "eligible_regimes": sorted(x.value for x in r.instance.eligible_regimes),
             "live": live.get(name, {})}
            for name, r in self.registry.records.items()
        ]

    def market_summary(self) -> dict:
        quality = self.quality.snapshot()
        out = {}
        for symbol in self.settings.all_symbols:
            micro = self.micro.features(symbol, self.clock.now())
            q = quality.get(symbol, {})
            out[symbol] = {
                "last_price": q.get("last_price"),
                "age_s": q.get("age_s"),
                "quality": q.get("quality", 0.0),
                "spread_pct": micro.get("spread_pct_last"),
                "quote_volume_24h": self.ticker_volumes.get(symbol),
                "regime": self.regimes.get(symbol, {}).get("regime"),
            }
        return out


def main() -> None:
    platform = Platform()
    try:
        asyncio.run(platform.run())
    except KeyboardInterrupt:  # pragma: no cover
        logging.getLogger(__name__).info("interrupted")


if __name__ == "__main__":
    main()
