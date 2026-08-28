"""Application entry point: tweet → classify → validate → trade session.

    python -m src.main

Modes (spec §13):
  PAPER (default) — live market data, simulated fills. Needs only X token.
  LIVE            — real Binance USDⓈ-M orders; requires ENABLE_LIVE_TRADING=true too.
  BACKTEST        — use the research CLIs instead (src.backtest.event_study /
                    src.backtest.simulator); this entry point refuses to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal as os_signal
import uuid

from src.core.clock import RealClock, utcnow
from src.core.config import ListenerMode, Mode, Settings, SignalSource, StrategyMode, get_settings
from src.core.domain import SkipReason, TweetEvent, TweetKind
from src.core.logger import get_logger, register_secret, setup_logging
from src.exchange.binance_client import BinanceFuturesClient
from src.exchange.symbol_mapper import SymbolMapper
from src.exchange.websocket import SymbolFeed
from src.execution.adapters import LiveAdapter, PaperAdapter
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.notifications.telegram import TelegramNotifier
from src.risk.kill_switch import KillSwitch
from src.risk.risk_manager import RiskManager
from src.storage.database import Database, Repo
from src.strategy.entry import EntryInputs
from src.strategy.session import TradeSession
from src.telegram_source.listener import TelegramSourceListener
from src.twitter.classifier import LlmClassifier, PhraseEdgeTable, SignalClassifier
from src.twitter.listener import TweetListener, XClient
from src.twitter.parser import extract_candidates

log = get_logger(__name__)


class App:
    def __init__(self, settings: Settings | None = None) -> None:
        self.cfg = settings or get_settings()
        self.clock = RealClock()
        self.db = Database(self.cfg.database_url)
        self.repo = Repo(self.db)
        self.kill = KillSwitch()
        self.risk = RiskManager(cfg=self.cfg, repo=self.repo, kill=self.kill)
        self.market = BinanceFuturesClient(self.cfg.fapi_url,
                                           api_key=self.cfg.binance_api_key,
                                           api_secret=self.cfg.binance_api_secret)
        self.mapper = SymbolMapper(self.market)
        self.positions = PositionManager(self.repo, self.clock, self.kill)
        self.notifier = TelegramNotifier(self.cfg.telegram_bot_token,
                                         self.cfg.telegram_chat_id)
        llm = (LlmClassifier(self.cfg.anthropic_api_key, self.cfg.llm_model)
               if self.cfg.anthropic_api_key else None)
        self.classifier = SignalClassifier(
            edge_table=PhraseEdgeTable(self.cfg.data_dir / "phrase_edge.json"), llm=llm)

        self.live_client: BinanceFuturesClient | None = None
        if self.cfg.live_execution_enabled:
            adapter = LiveAdapter(self.cfg, self.clock)
            self.live_client = adapter.client
            log.warning("⚠️  LIVE trading ENABLED (MODE=LIVE + ENABLE_LIVE_TRADING=true)")
        else:
            adapter = PaperAdapter(self.cfg, self.clock)
            log.info("execution adapter: PAPER (simulated fills on live data)")
        self.orders = OrderManager(adapter, self.repo, self.clock)
        self.sessions: dict[str, TradeSession] = {}  # symbol → active session
        self._tasks: set[asyncio.Task] = set()

    # ── startup / shutdown ───────────────────────────────────────────────────

    async def start(self) -> None:
        setup_logging(self.cfg.log_level)
        for secret in self.cfg.secrets():
            register_secret(secret)
        if self.cfg.mode == Mode.BACKTEST:
            raise SystemExit("MODE=BACKTEST: use `python -m src.backtest.event_study` "
                             "or `python -m src.backtest.simulator`")
        wants_x = self.cfg.signal_source in (SignalSource.X, SignalSource.BOTH)
        wants_tg = self.cfg.signal_source in (SignalSource.TELEGRAM, SignalSource.BOTH)
        if wants_x and not self.cfg.x_bearer_token:
            raise SystemExit("SIGNAL_SOURCE includes X but X_BEARER_TOKEN is missing")
        if wants_tg and not (self.cfg.telegram_api_id and self.cfg.telegram_api_hash
                             and self.cfg.telegram_session and self.cfg.tg_channels):
            raise SystemExit("SIGNAL_SOURCE includes TELEGRAM: set TELEGRAM_API_ID/HASH, "
                             "run `python -m src.telegram_source.login` for "
                             "TELEGRAM_SESSION, and configure TG_SOURCE_CHANNELS")
        await self.db.create_all()
        await self.risk.restore(self.clock.now())
        if not await self.market.ping():
            self.kill.trip("EXCHANGE_API_PROBLEM", "fapi unreachable at startup")
        else:
            await self.mapper.refresh(self.clock.now())
        await self.positions.restore_and_reconcile(self.clock.now(),
                                                   live_client=self.live_client)
        await self.orders.reconcile_unknown(self.live_client)
        if self.kill.active:
            await self.notifier.kill_switch(self.kill.reasons)
        log.info("started in %s mode (live=%s), %d known futures bases",
                 self.cfg.mode, self.cfg.live_execution_enabled,
                 len(self.mapper.known_bases))

    def _build_listeners(self) -> list:
        listeners = []
        if self.cfg.signal_source in (SignalSource.X, SignalSource.BOTH):
            listeners.append(("X_FEED_PROBLEM", TweetListener(
                XClient(self.cfg.x_bearer_token, api_base=self.cfg.x_api_base),
                self.cfg.x_target_username, self.on_tweet,
                mode=self.cfg.x_listener_mode.value
                if isinstance(self.cfg.x_listener_mode, ListenerMode) else "auto",
                poll_interval_s=self.cfg.x_poll_interval_seconds)))
        if self.cfg.signal_source in (SignalSource.TELEGRAM, SignalSource.BOTH):
            listeners.append(("TG_FEED_PROBLEM", TelegramSourceListener(
                api_id=self.cfg.telegram_api_id, api_hash=self.cfg.telegram_api_hash,
                session=self.cfg.telegram_session, channels=self.cfg.tg_channels,
                on_message=self.on_tweet)))
        return listeners

    async def run(self) -> None:
        await self.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        async def supervise(kill_key: str, listener) -> None:
            backoff = 1.0
            while not stop.is_set():
                try:
                    self.kill.clear(kill_key)
                    await listener.run()
                except Exception as exc:
                    self.kill.trip(kill_key, str(exc)[:150])
                    log.exception("listener crashed (%s); restart in %.0fs",
                                  kill_key, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        tasks = [asyncio.create_task(supervise(key, lst))
                 for key, lst in self._build_listeners()]
        await stop.wait()
        for task in tasks:
            task.cancel()
        for t in list(self._tasks):
            t.cancel()
        await self.db.dispose()
        log.info("shutdown complete")

    # ── the pipeline ─────────────────────────────────────────────────────────

    async def on_tweet(self, tweet: TweetEvent) -> None:
        if not await self.repo.store_tweet(tweet):
            log.info("duplicate tweet %s ignored", tweet.tweet_id)
            return
        if tweet.kind != TweetKind.ORIGINAL:
            log.info("ignoring %s tweet %s", tweet.kind, tweet.tweet_id)
            return
        await self.mapper.ensure_fresh(self.clock.now())
        candidates = extract_candidates(tweet.text, self.mapper.known_bases)
        classification = await self.classifier.classify(tweet, candidates)
        await self.repo.store_signal(classification, self.clock.now())
        await self.notifier.new_tweet(tweet, classification)
        if not classification.is_trade_signal:
            await self.repo.mark_signal_skipped(tweet.tweet_id, SkipReason.NOT_TRADE_SIGNAL)
            return
        rules = self.mapper.resolve([classification.symbol] if classification.symbol else [])
        if rules is None:
            await self.repo.mark_signal_skipped(tweet.tweet_id,
                                                SkipReason.SYMBOL_NOT_ON_BINANCE)
            log.info("symbol %s not tradable on futures — SKIP", classification.symbol)
            return
        await self.notifier.signal_detected(tweet, classification, rules.symbol)
        if rules.symbol in self.sessions and not self.sessions[rules.symbol].done:
            await self.repo.mark_signal_skipped(tweet.tweet_id, SkipReason.DUPLICATE)
            log.info("session already active on %s — SKIP", rules.symbol)
            return
        task = asyncio.create_task(self._run_session(tweet, classification, rules))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reference_price(self, symbol: str, tweet: TweetEvent) -> float | None:
        """Price as close as possible to the tweet timestamp (first trade ≥ t)."""
        try:
            tweet_ms = int(tweet.created_at.timestamp() * 1000)
            trades = await self.market.agg_trades(symbol, start_ms=tweet_ms, limit=1)
            if trades:
                return float(trades[0]["p"])
        except Exception:
            log.exception("reference price lookup failed; falling back to mid")
        return None

    async def _run_session(self, tweet: TweetEvent, classification, rules) -> None:
        symbol = rules.symbol
        session = TradeSession(
            session_id=str(uuid.uuid4()), tweet=tweet, classification=classification,
            rules=rules, cfg=self.cfg, clock=self.clock, orders=self.orders,
            positions=self.positions, risk=self.risk, repo=self.repo,
            notifier=self.notifier)
        self.sessions[symbol] = session
        feed = SymbolFeed(self.cfg.fstream_base, symbol, on_trade=session.on_trade,
                          on_book=session.on_book, on_depth=session.on_depth)
        feed.start()
        try:
            # gather validation inputs
            book = await self.market.book_ticker(symbol)
            bid, ask = float(book["bidPrice"]), float(book["askPrice"])
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 99.0
            ticker = await self.market.ticker_24h(symbol)
            depth = await self.market.depth(symbol, limit=5)
            bid_liq = sum(float(p) * float(q) for p, q in depth.get("bids", [])[:5])
            ask_liq = sum(float(p) * float(q) for p, q in depth.get("asks", [])[:5])
            reference = await self._reference_price(symbol, tweet) or mid
            if self.live_client is not None:
                with contextlib.suppress(Exception):
                    await self.live_client.set_leverage(symbol, self.cfg.max_leverage)
            inputs = EntryInputs(
                now=self.clock.now(), reference_price=reference, mid_price=mid,
                spread_pct=spread_pct,
                volume_24h_quote=float(ticker.get("quoteVolume", 0.0)),
                bid_liquidity_usdt=bid_liq, ask_liquidity_usdt=ask_liq,
                feed_staleness_s=feed.staleness_seconds(utcnow()))
            if self.cfg.strategy_mode == StrategyMode.SHORT_ONLY:
                max_age = (self.cfg.tg_max_message_age_seconds
                           if tweet.tweet_id.startswith("tg:")
                           else self.cfg.max_tweet_age_seconds)
                started = await session.start_watch(inputs, max_age_seconds=max_age)
            else:
                started = await session.start(inputs)
            if not started:
                return
            # manage the open session until terminal; watchdog for stale feeds
            while not session.done:
                await asyncio.sleep(1.0)
                staleness = feed.staleness_seconds(utcnow())
                self.kill.check_feed(staleness, self.cfg.max_data_staleness_seconds * 4)
                if staleness > self.cfg.max_data_staleness_seconds * 4:
                    log.warning("feed stale %.1fs during open session on %s",
                                staleness, symbol)
                    await self.notifier.kill_switch(self.kill.reasons)
                # periodic time-based evaluation even without fresh trades
                await session.evaluate(self.clock.now())
        except Exception:
            log.exception("session on %s crashed", symbol)
            self.kill.trip("ORDER_STATE_UNCERTAIN", f"session crash on {symbol}")
            await self.notifier.kill_switch(self.kill.reasons)
        finally:
            await feed.stop()
            self.sessions.pop(symbol, None)


def main() -> None:
    app = App()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(app.run())


if __name__ == "__main__":
    main()
