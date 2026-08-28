"""X/Twitter listener (spec §2): lowest-latency available method, with fallback.

- STREAM: X API v2 Filtered Stream (`GET /2/tweets/search/stream`) with a
  `from:<user> -is:retweet -is:reply` rule. Requires an access tier that
  includes streaming (Pro). Latency: sub-second to a few seconds.
- POLL: `GET /2/users/:id/tweets` with `since_id` on an interval. Works on
  lower tiers; latency ≈ poll interval + API lag. Rate-limit aware.
- AUTO: try STREAM; on 402/403 (tier does not include streaming) fall back to POLL.

Every tweet is timestamped on receipt; `twitter_latency_ms` is recorded.
Duplicates are dropped by tweet_id here AND at the DB unique constraint.
Replies/quotes/retweets are labeled and (by default) not treated as signals.

NOTE: docs.x.com was unreachable from the build environment; endpoints follow
the X API v2 surface (api.x.com, with api.twitter.com as the legacy alias).
Verify your tier's rate limits in the X developer portal before going live.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from src.core.clock import utcnow
from src.core.domain import TweetEvent, TweetKind
from src.core.logger import get_logger, log_ctx, register_secret

log = get_logger(__name__)

TWEET_FIELDS = "created_at,author_id,referenced_tweets,in_reply_to_user_id,entities"


def _parse_created_at(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def tweet_kind(data: dict) -> TweetKind:
    refs = data.get("referenced_tweets") or []
    types = {r.get("type") for r in refs}
    if "retweeted" in types:
        return TweetKind.RETWEET
    if "replied_to" in types or data.get("in_reply_to_user_id"):
        return TweetKind.REPLY
    if "quoted" in types:
        return TweetKind.QUOTE
    return TweetKind.ORIGINAL


class XApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class XClient:
    def __init__(self, bearer_token: str, *, api_base: str = "https://api.x.com",
                 client: httpx.AsyncClient | None = None) -> None:
        register_secret(bearer_token)
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self._client.get(f"{self.api_base}{path}", params=params,
                                      headers=self._headers)
        if resp.status_code == 429:
            reset = float(resp.headers.get("x-rate-limit-reset", "0"))
            wait = max(5.0, reset - utcnow().timestamp()) if reset else 60.0
            log.warning("X API rate limited; sleeping %.0fs", min(wait, 900))
            await asyncio.sleep(min(wait, 900))
            raise XApiError("rate limited", status=429)
        if resp.status_code >= 400:
            raise XApiError(f"X API {resp.status_code}: {resp.text[:200]}",
                            status=resp.status_code)
        return resp.json()

    async def get_user_id(self, username: str) -> str:
        data = await self._get(f"/2/users/by/username/{username}")
        return str(data["data"]["id"])

    async def user_tweets(self, user_id: str, *, since_id: str | None = None,
                          max_results: int = 5) -> list[dict]:
        params: dict = {"max_results": max(5, min(max_results, 100)),
                        "exclude": "retweets,replies",
                        "tweet.fields": TWEET_FIELDS}
        if since_id:
            params["since_id"] = since_id
        data = await self._get(f"/2/users/{user_id}/tweets", params)
        return data.get("data") or []

    async def set_stream_rules(self, username: str) -> None:
        """Replace all rules with a single from:<user> rule (no replies/retweets)."""
        existing = await self._get("/2/tweets/search/stream/rules")
        ids = [r["id"] for r in existing.get("data") or []]
        if ids:
            await self._client.post(f"{self.api_base}/2/tweets/search/stream/rules",
                                    headers=self._headers,
                                    json={"delete": {"ids": ids}})
        resp = await self._client.post(
            f"{self.api_base}/2/tweets/search/stream/rules", headers=self._headers,
            json={"add": [{"value": f"from:{username} -is:retweet -is:reply",
                           "tag": f"eliz:{username}"}]})
        if resp.status_code >= 400:
            raise XApiError(f"rule setup failed {resp.status_code}: {resp.text[:200]}",
                            status=resp.status_code)

    def stream(self):
        """Async context manager yielding the raw filtered-stream response."""
        params = {"tweet.fields": TWEET_FIELDS}
        return self._client.stream("GET", f"{self.api_base}/2/tweets/search/stream",
                                   params=params, headers=self._headers, timeout=90.0)

    async def close(self) -> None:
        await self._client.aclose()


OnTweet = Callable[[TweetEvent], Awaitable[None]]


class TweetListener:
    """Facade: stream with automatic fallback to polling (mode: auto|stream|poll)."""

    def __init__(self, client: XClient, username: str, on_tweet: OnTweet, *,
                 mode: str = "auto", poll_interval_s: float = 8.0) -> None:
        self.client = client
        self.username = username
        self.on_tweet = on_tweet
        self.mode = mode
        self.poll_interval_s = poll_interval_s
        self._seen_ids: set[str] = set()
        self._since_id: str | None = None
        self._user_id: str | None = None
        self.active_transport: str = "none"
        self.last_event_at: datetime | None = None
        self.healthy: bool = False

    async def _emit(self, data: dict) -> None:
        tweet_id = str(data.get("id", ""))
        if not tweet_id or tweet_id in self._seen_ids:
            return  # duplicate event
        self._seen_ids.add(tweet_id)
        if len(self._seen_ids) > 10_000:
            self._seen_ids = set(list(self._seen_ids)[-2_000:])
        created_raw = data.get("created_at")
        if not created_raw:
            return
        event = TweetEvent(
            tweet_id=tweet_id, author_id=str(data.get("author_id", "")),
            text=data.get("text", ""), kind=tweet_kind(data),
            created_at=_parse_created_at(created_raw), received_at=utcnow(), raw=data)
        self.last_event_at = event.received_at
        log_ctx(log, logging.INFO, "tweet received", tweet_id=tweet_id,
                latency_ms=round(event.latency_ms, 1), kind=event.kind.value)
        await self.on_tweet(event)

    # ── transports ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Supervised entry point: chooses transport, reconnects forever."""
        if self.mode in ("auto", "stream"):
            try:
                await self._run_stream()
                return
            except XApiError as exc:
                if self.mode == "stream" or exc.status not in (402, 403):
                    raise
                log.warning("filtered stream unavailable on this tier (%s); "
                            "falling back to polling", exc.status)
        await self._run_poll()

    async def _run_stream(self) -> None:
        await self.client.set_stream_rules(self.username)
        self.active_transport = "stream"
        async with self.client.stream() as resp:
            if resp.status_code in (402, 403):
                raise XApiError("stream not available on this access tier",
                                status=resp.status_code)
            if resp.status_code >= 400:
                body = await resp.aread()
                raise XApiError(f"stream error {resp.status_code}: {body[:200]!r}",
                                status=resp.status_code)
            self.healthy = True
            log.info("filtered stream connected for @%s", self.username)
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue  # keep-alive newline
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = payload.get("data")
                if isinstance(data, dict):
                    await self._emit(data)

    async def _run_poll(self) -> None:
        self.active_transport = "poll"
        if self._user_id is None:
            self._user_id = await self.client.get_user_id(self.username)
        self.healthy = True
        log.info("polling timeline of @%s every %.0fs (higher latency than stream)",
                 self.username, self.poll_interval_s)
        while True:
            try:
                tweets = await self.client.user_tweets(self._user_id,
                                                       since_id=self._since_id)
            except XApiError as exc:
                if exc.status == 429:
                    continue  # already slept inside the client
                raise
            for data in sorted(tweets, key=lambda t: t["id"]):
                self._since_id = max(self._since_id or "0", str(data["id"]), key=int)
                await self._emit(data)
            await asyncio.sleep(self.poll_interval_s)
