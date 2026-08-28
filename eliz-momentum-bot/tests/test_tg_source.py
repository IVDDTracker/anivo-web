"""Telegram source: message conversion, dedup, classifier compatibility."""

from __future__ import annotations

from datetime import UTC, datetime

from src.core.domain import TweetKind
from src.telegram_source.listener import TelegramSourceListener, message_to_event
from src.twitter.classifier import SignalClassifier
from src.twitter.parser import extract_candidates

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class TestMessageToEvent:
    def test_basic_conversion(self):
        e = message_to_event(chat_id=-100123, message_id=55, text="Bought $TAO here",
                             date=T0, channel_name="pumpwatch")
        assert e.tweet_id == "tg:-100123:55"
        assert e.author_id == "pumpwatch"
        assert e.kind == TweetKind.ORIGINAL
        assert e.created_at == T0 and e.latency_ms >= 0

    def test_naive_date_becomes_utc(self):
        e = message_to_event(chat_id=1, message_id=2, text="hello $TAO",
                             date=T0.replace(tzinfo=None))
        assert e.created_at.tzinfo is not None

    def test_empty_or_tiny_messages_dropped(self):
        assert message_to_event(chat_id=1, message_id=2, text="", date=T0) is None
        assert message_to_event(chat_id=1, message_id=2, text="ok", date=T0) is None

    async def test_pipeline_compatibility(self):
        """A telegram message flows through the SAME classifier as a tweet."""
        e = message_to_event(chat_id=-1, message_id=9,
                             text="🚀 $TAO breakout soon, watching this",
                             date=T0, channel_name="signals")
        c = await SignalClassifier().classify(e, extract_candidates(e.text, {"TAO"}))
        assert c.is_trade_signal and c.signal_stage.value == "EARLY"


class TestListenerDedup:
    async def test_duplicate_messages_emitted_once(self):
        received = []

        async def on_message(e):
            received.append(e.tweet_id)

        listener = TelegramSourceListener(
            api_id=1, api_hash="h", session="s", channels=["@c"], on_message=on_message)
        event = message_to_event(chat_id=-1, message_id=7, text="pump $TAO now",
                                 date=T0)
        await listener._emit(event)
        await listener._emit(event)               # duplicate delivery
        await listener._emit(None)                # unusable message
        assert received == ["tg:-1:7"]

    def test_requires_credentials_and_channels(self):
        import pytest

        async def noop(e):
            return None

        with pytest.raises(ValueError):
            TelegramSourceListener(api_id=0, api_hash="", session="", channels=["@c"],
                                   on_message=noop)
        with pytest.raises(ValueError):
            TelegramSourceListener(api_id=1, api_hash="h", session="s", channels=[],
                                   on_message=noop)
