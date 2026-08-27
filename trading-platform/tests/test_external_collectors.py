"""GitHub + RSS collector tests with mocked HTTP (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config.sources import GitHubSourceCfg, RssSourceCfg
from app.core.bus import EventBus, Topics
from app.data.collectors.github import GitHubCollector
from app.data.collectors.rss import FearGreedProvider, RssProvider, parse_feed
from app.models.enums import EventCategory

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item><guid>g1</guid><title>Exchange lists SOL futures</title>
<link>https://example.com/1</link>
<description>Big listing news</description>
<pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate></item>
<item><guid>g2</guid><title>Network outage on Solana, chain halt</title>
<link>https://example.com/2</link>
<pubDate>Wed, 26 Aug 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><id>a1</id><title>Protocol mainnet upgrade release</title>
<link href="https://blog.example.com/1"/>
<published>2026-08-26T10:00:00Z</published></entry></feed>"""


class TestFeedParsing:
    def test_rss(self):
        items = parse_feed(RSS_XML)
        assert len(items) == 2 and items[0]["title"].startswith("Exchange lists")
        assert items[0]["published"] is not None

    def test_atom(self):
        items = parse_feed(ATOM_XML)
        assert len(items) == 1 and items[0]["link"] == "https://blog.example.com/1"

    def test_malformed_raises(self):
        import xml.etree.ElementTree as ET

        with pytest.raises((ET.ParseError, ValueError)):
            parse_feed("<html>not a feed</html>")


class TestRssProvider:
    @respx.mock
    async def test_fetch_normalize_and_dedup(self):
        respx.get("https://example.com/feed").mock(
            return_value=httpx.Response(200, text=RSS_XML))
        cfg = RssSourceCfg(name="test", url="https://example.com/feed", category="news",
                           reliability_score=0.7)
        provider = RssProvider(cfg)
        items = await provider.fetch()
        events = provider.normalize(items)
        assert len(events) == 2
        assert events[0].category == EventCategory.LISTING
        assert events[1].category == EventCategory.OUTAGE
        assert "SOLUSDT" in events[0].assets
        assert await provider.healthcheck()
        # second fetch: nothing new
        assert await provider.fetch() == []

    @respx.mock
    async def test_failure_marks_unhealthy(self):
        respx.get("https://example.com/feed").mock(return_value=httpx.Response(500))
        cfg = RssSourceCfg(name="bad", url="https://example.com/feed")
        provider = RssProvider(cfg)
        assert await provider.fetch() == []
        assert not await provider.healthcheck()

    @respx.mock
    async def test_fear_greed(self):
        respx.get("https://api.alternative.me/fng/?limit=1").mock(
            return_value=httpx.Response(200, json={"data": [
                {"value": "20", "value_classification": "Extreme Fear"}]}))
        provider = FearGreedProvider()
        events = provider.normalize(await provider.fetch())
        assert len(events) == 1 and events[0].sentiment < 0


GH_RELEASES = [{"id": 7, "name": "v25.0", "tag_name": "v25.0", "draft": False,
                "published_at": "2026-08-25T12:00:00Z",
                "html_url": "https://github.com/bitcoin/bitcoin/releases/v25.0"}]
GH_COMMITS = [
    {"sha": "abc", "html_url": "u1",
     "commit": {"message": "fix: critical consensus bug", "committer": {"date": "2026-08-25T10:00:00Z"}}},
    {"sha": "def", "html_url": "u2",
     "commit": {"message": "docs: typo", "committer": {"date": "2026-08-25T09:00:00Z"}}},
]


class TestGitHubCollector:
    def make(self):
        bus = EventBus()
        cfg = GitHubSourceCfg(name="btc", repo="bitcoin/bitcoin", assets=["BTCUSDT"],
                              reliability_score=0.7)
        collector = GitHubCollector([cfg], bus, api_base="https://api.github.com")
        return collector, cfg, bus

    @respx.mock
    async def test_releases_advisories_commits(self):
        collector, cfg, bus = self.make()
        sub = bus.subscribe(Topics.EXTERNAL_EVENT, name="t")
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/releases").mock(
            return_value=httpx.Response(200, json=GH_RELEASES))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/security-advisories").mock(
            return_value=httpx.Response(200, json=[
                {"ghsa_id": "GHSA-xxxx", "severity": "high", "summary": "RCE in RPC",
                 "published_at": "2026-08-25T12:00:00Z", "html_url": "u"}]))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/commits").mock(
            return_value=httpx.Response(200, json=GH_COMMITS))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin").mock(
            return_value=httpx.Response(200, json={"archived": False}))
        events = await collector.poll_repo(cfg)
        categories = {e.category for e in events}
        assert EventCategory.RELEASE in categories
        assert EventCategory.SECURITY_ADVISORY in categories
        assert EventCategory.DEVELOPMENT in categories  # only the notable commit
        assert all(e.assets == ["BTCUSDT"] for e in events)
        assert sub.queue.qsize() == len(events)
        # release sentiment must not be assumed bullish beyond mild default
        release = next(e for e in events if e.category == EventCategory.RELEASE)
        assert release.sentiment <= 0.5

    @respx.mock
    async def test_second_poll_no_duplicates(self):
        collector, cfg, _ = self.make()
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/releases").mock(
            return_value=httpx.Response(200, json=GH_RELEASES))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/security-advisories").mock(
            return_value=httpx.Response(200, json=[]))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin/commits").mock(
            return_value=httpx.Response(200, json=GH_COMMITS))
        respx.get("https://api.github.com/repos/bitcoin/bitcoin").mock(
            return_value=httpx.Response(200, json={"archived": False}))
        first = await collector.poll_repo(cfg)
        second = await collector.poll_repo(cfg)
        assert first and second == []

    @respx.mock
    async def test_error_status_tolerated(self):
        collector, cfg, _ = self.make()
        respx.get(url__regex=r"https://api\.github\.com/.*").mock(
            return_value=httpx.Response(403, json={"message": "forbidden"}))
        assert await collector.poll_repo(cfg) == []
