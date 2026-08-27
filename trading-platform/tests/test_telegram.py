"""Telegram tests: admin-only control, low-trust ingestion, notifier dedup."""

from __future__ import annotations

from datetime import timedelta

from app.config.sources import TelegramSourceCfg
from app.core.bus import EventBus, Topics
from app.data.normalization.events import (
    build_external_event,
    classify_category,
    extract_assets,
    naive_sentiment,
)
from app.models.enums import EventCategory, SourceType
from app.telegram.bot import CommandRouter, TelegramBot
from app.telegram.ingest import TelegramIngest
from app.telegram.notifier import Notifier


class FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, silent=False):
        self.sent.append((chat_id, text))

    async def get_updates(self, offset, timeout_s=50):
        return []


def make_bot(ingest=None, ingest_chats=None):
    router = CommandRouter()

    async def status(_args: str) -> str:
        return "state: HEALTHY"

    router.register("/status", status)
    client = FakeClient()
    bot = TelegramBot(client, admin_chat_id="111", router=router,
                      ingest_handler=ingest, ingest_chat_ids=ingest_chats or set())
    return bot, client


def msg(chat_id, text, date=1_760_000_000):
    return {"message": {"chat": {"id": chat_id}, "text": text, "date": date}}


class TestControlBot:
    async def test_admin_command_answered(self):
        bot, client = make_bot()
        await bot.handle_update(msg(111, "/status"))
        assert client.sent and "HEALTHY" in client.sent[0][1]

    async def test_unknown_command_lists_available(self):
        bot, client = make_bot()
        await bot.handle_update(msg(111, "/bogus"))
        assert "Unknown command" in client.sent[0][1]

    async def test_non_admin_chat_ignored_for_control(self):
        bot, client = make_bot()
        await bot.handle_update(msg(222, "/status"))
        assert client.sent == []

    async def test_ingest_chat_never_dispatches_commands(self):
        captured = []

        async def ingest(message):
            captured.append(message)

        bot, client = make_bot(ingest=ingest, ingest_chats={"333"})
        await bot.handle_update(msg(333, "/status"))
        assert client.sent == []  # ingestion chats can't run commands
        assert captured  # but the text is captured as data


class TestIngestion:
    def make_ingest(self):
        bus = EventBus()
        sources = [TelegramSourceCfg(name="alpha", identifier="333", reliability_score=0.9,
                                     keywords=["hack", "listing"], symbols=["BTCUSDT"])]
        return TelegramIngest(sources, bus, reliability_cap=0.35), bus

    async def test_reliability_hard_capped(self):
        ingest, bus = self.make_ingest()
        sub = bus.subscribe(Topics.EXTERNAL_EVENT, name="t")
        event = await ingest.handle_message(
            msg(333, "BREAKING: major exchange hack, BTC moving")["message"])
        assert event is not None
        assert event.reliability <= 0.35  # configured 0.9 is capped: Telegram is LOW TRUST
        assert sub.queue.qsize() == 1

    async def test_keyword_filter(self):
        ingest, _ = self.make_ingest()
        assert await ingest.handle_message(msg(333, "gm frens, nice weather")["message"]) is None

    async def test_unconfigured_chat_dropped(self):
        ingest, _ = self.make_ingest()
        assert await ingest.handle_message(msg(999, "huge hack news")["message"]) is None

    async def test_event_categorized_and_asset_mapped(self):
        ingest, _ = self.make_ingest()
        event = await ingest.handle_message(
            msg(333, "Bitcoin bridge hacked, funds drained")["message"])
        assert event.category == EventCategory.HACK
        assert event.assets == ["BTCUSDT"]
        assert event.sentiment < 0


class TestClassification:
    def test_categories(self):
        assert classify_category("Exchange will list SOL perpetuals") == EventCategory.LISTING
        assert classify_category("SEC files lawsuit against exchange") == EventCategory.REGULATORY
        assert classify_category("Solana network outage, chain halt") == EventCategory.OUTAGE
        assert classify_category("v2.1.0 release with mainnet upgrade") == EventCategory.RELEASE
        assert classify_category("nothing interesting today") == EventCategory.OTHER

    def test_asset_extraction(self):
        assert extract_assets("Ethereum and SOL rally while btc consolidates") == \
               ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert extract_assets("nothing about crypto here") == []

    def test_sentiment(self):
        assert naive_sentiment("massive rally and adoption growth") > 0
        assert naive_sentiment("hack causes crash and liquidation") < 0

    def test_rumor_has_short_half_life(self, sim_clock):
        event = build_external_event(
            text="unconfirmed rumor: exchange X insolvent, sources say",
            source="telegram:x", source_type=SourceType.TELEGRAM, reliability=0.3,
            timestamp=sim_clock.now())
        assert event.category == EventCategory.RUMOR
        assert event.decay_half_life_hours <= 2.0
        fresh = event.decayed_weight(sim_clock.now())
        stale = event.decayed_weight(sim_clock.now() + timedelta(hours=12))
        assert stale < fresh / 50  # a 12h old rumor is noise


class TestNotifier:
    def test_dedupe_window(self, sim_clock):
        notifier = Notifier(client=FakeClient(), chat_id="111", clock=sim_clock)
        notifier.risk_alert("daily_loss", "limit hit")
        notifier.risk_alert("daily_loss", "limit hit")
        assert notifier._queue.qsize() == 1
        sim_clock.advance_to(sim_clock.now() + timedelta(seconds=400))
        notifier.risk_alert("daily_loss", "limit hit")
        assert notifier._queue.qsize() == 2

    def test_disabled_without_config(self, sim_clock):
        notifier = Notifier(client=None, chat_id="", clock=sim_clock)
        notifier.risk_alert("x", "y")  # no crash, no queue growth
        assert notifier._queue.qsize() == 0
