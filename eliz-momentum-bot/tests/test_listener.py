"""X listener: polling transport, dedup, kind separation, stream fallback."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from src.core.domain import TweetKind
from src.twitter.listener import TweetListener, XApiError, XClient, tweet_kind

BASE = "https://api.x.com"


def make_listener(events, mode="poll"):
    client = XClient("token", api_base=BASE)

    async def on_tweet(t):
        events.append(t)

    return TweetListener(client, "eliz883", on_tweet, mode=mode, poll_interval_s=0.01)


def tweet_payload(tid: str, text: str, **extra):
    return {"id": tid, "text": text, "author_id": "99",
            "created_at": "2026-03-01T12:00:00.000Z", **extra}


class TestKindDetection:
    def test_kinds(self):
        assert tweet_kind({}) == TweetKind.ORIGINAL
        assert tweet_kind({"referenced_tweets": [{"type": "retweeted"}]}) == TweetKind.RETWEET
        assert tweet_kind({"referenced_tweets": [{"type": "quoted"}]}) == TweetKind.QUOTE
        assert tweet_kind({"in_reply_to_user_id": "5"}) == TweetKind.REPLY


class TestPolling:
    @respx.mock
    async def test_poll_emits_with_latency_and_dedup(self):
        respx.get(f"{BASE}/2/users/by/username/eliz883").mock(
            return_value=httpx.Response(200, json={"data": {"id": "77"}}))
        calls = {"n": 0}

        def timeline(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"data": [
                    tweet_payload("101", "bought $TAO"),
                    tweet_payload("100", "gm")]})
            return httpx.Response(200, json={"data": [
                tweet_payload("101", "bought $TAO"),      # duplicate again
                tweet_payload("102", "$SOL watching")]})

        respx.get(url__regex=rf"{BASE}/2/users/77/tweets.*").mock(side_effect=timeline)
        events = []
        listener = make_listener(events)
        task = asyncio.create_task(listener._run_poll())
        for _ in range(200):
            if len(events) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        ids = [e.tweet_id for e in events]
        assert ids == ["100", "101", "102"]  # chronological, no duplicate 101
        assert all(e.latency_ms >= 0 for e in events)
        assert listener.active_transport == "poll"

    @respx.mock
    async def test_auto_falls_back_when_stream_forbidden(self):
        respx.post(url__regex=rf"{BASE}/2/tweets/search/stream/rules.*").mock(
            return_value=httpx.Response(403, json={"detail": "tier"}))
        respx.get(f"{BASE}/2/tweets/search/stream/rules").mock(
            return_value=httpx.Response(403, json={"detail": "tier"}))
        respx.get(f"{BASE}/2/users/by/username/eliz883").mock(
            return_value=httpx.Response(200, json={"data": {"id": "77"}}))
        respx.get(url__regex=rf"{BASE}/2/users/77/tweets.*").mock(
            return_value=httpx.Response(200, json={"data": [
                tweet_payload("300", "hello")]}))
        events = []
        listener = make_listener(events, mode="auto")
        task = asyncio.create_task(listener.run())
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        assert listener.active_transport == "poll"
        assert events and events[0].tweet_id == "300"

    @respx.mock
    async def test_stream_mode_does_not_fall_back(self):
        respx.get(f"{BASE}/2/tweets/search/stream/rules").mock(
            return_value=httpx.Response(403, json={}))
        listener = make_listener([], mode="stream")
        with pytest.raises(XApiError):
            await listener.run()
        assert listener.active_transport != "poll"
