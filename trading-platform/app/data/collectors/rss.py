"""Generic external data provider system + RSS/Atom and Fear&Greed providers.

`ExternalDataProvider` is the plug-in interface: fetch() → normalize() →
ExternalEvents; healthcheck(); score_reliability(). Feeds are parsed with the
stdlib XML parser; malformed feeds are dropped and the provider marked unhealthy.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.config.sources import RssSourceCfg
from app.core.bus import EventBus, Topics
from app.core.logging import get_logger
from app.data.collectors.base import BaseCollector
from app.data.normalization.events import build_external_event
from app.models.enums import EventCategory, SourceType
from app.models.events import ExternalEvent

log = get_logger(__name__)

_CATEGORY_RELIABILITY_FLOOR = {
    "exchange_official": 0.95,
    "project_official": 0.90,
    "regulatory": 0.90,
    "news": 0.70,
}


class ExternalDataProvider(ABC):
    name: str = "provider"

    @abstractmethod
    async def fetch(self) -> list[dict]: ...

    @abstractmethod
    def normalize(self, items: list[dict]) -> list[ExternalEvent]: ...

    @abstractmethod
    async def healthcheck(self) -> bool: ...

    @abstractmethod
    def score_reliability(self) -> float: ...


class RssProvider(ExternalDataProvider):
    def __init__(self, cfg: RssSourceCfg, *, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.name = f"rss:{cfg.name}"
        self._client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self._seen_ids: set[str] = set()
        self._last_ok = False

    def score_reliability(self) -> float:
        floor = _CATEGORY_RELIABILITY_FLOOR.get(self.cfg.category, 0.4)
        return min(max(self.cfg.reliability_score, 0.0), max(floor, self.cfg.reliability_score))

    async def healthcheck(self) -> bool:
        return self._last_ok

    async def fetch(self) -> list[dict]:
        try:
            resp = await self._client.get(self.cfg.url)
            resp.raise_for_status()
            items = parse_feed(resp.text)
            self._last_ok = True
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            self._last_ok = False
            log.warning("rss fetch failed for %s: %s", self.cfg.name, str(exc)[:200])
            return []
        fresh = []
        for item in items:
            key = item.get("id") or item.get("link") or item.get("title", "")
            if not key or key in self._seen_ids:
                continue
            self._seen_ids.add(key)
            fresh.append(item)
        if len(self._seen_ids) > 20_000:
            self._seen_ids = set(list(self._seen_ids)[-5_000:])
        return fresh

    def normalize(self, items: list[dict]) -> list[ExternalEvent]:
        events = []
        for item in items:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            text = title
            if summary := (item.get("summary") or "").strip():
                text = f"{title}\n{summary[:500]}"
            category = None
            if self.cfg.category == "regulatory":
                category = EventCategory.REGULATORY
            events.append(build_external_event(
                text=text, source=self.name, source_type=SourceType.NEWS,
                reliability=self.score_reliability(),
                timestamp=item.get("published") or datetime.now(UTC),
                url=item.get("link", ""), category=category,
                assets=self.cfg.assets or None,
            ))
        return events


def parse_feed(xml_text: str) -> list[dict]:
    """Minimal RSS 2.0 / Atom parser. Raises on malformed XML."""
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    if root.tag in ("rss", "rdf:RDF") or root.find("channel") is not None:
        for item in root.findall(".//item"):
            items.append({
                "id": _text(item, "guid") or _text(item, "link"),
                "title": _text(item, "title"),
                "link": _text(item, "link"),
                "summary": _text(item, "description"),
                "published": _parse_date(_text(item, "pubDate")),
            })
    elif root.tag == f"{ns_atom}feed":
        for entry in root.findall(f"{ns_atom}entry"):
            link_el = entry.find(f"{ns_atom}link")
            items.append({
                "id": _text(entry, f"{ns_atom}id"),
                "title": _text(entry, f"{ns_atom}title"),
                "link": link_el.get("href", "") if link_el is not None else "",
                "summary": _text(entry, f"{ns_atom}summary") or _text(entry, f"{ns_atom}content"),
                "published": _parse_date(
                    _text(entry, f"{ns_atom}published") or _text(entry, f"{ns_atom}updated")),
            })
    else:
        raise ValueError(f"unrecognized feed root {root.tag!r}")
    return items


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class FearGreedProvider(ExternalDataProvider):
    """Alternative.me Fear & Greed index → SENTIMENT event (feature only)."""

    name = "sentiment:fear_greed"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._last_ok = False
        self._last_value: int | None = None

    def score_reliability(self) -> float:
        return 0.5

    async def healthcheck(self) -> bool:
        return self._last_ok

    async def fetch(self) -> list[dict]:
        try:
            resp = await self._client.get("https://api.alternative.me/fng/?limit=1")
            resp.raise_for_status()
            data = resp.json().get("data", [])
            self._last_ok = True
        except (httpx.HTTPError, ValueError):
            self._last_ok = False
            return []
        if not data:
            return []
        value = int(data[0].get("value", 50))
        if value == self._last_value:
            return []
        self._last_value = value
        return [{"value": value, "classification": data[0].get("value_classification", "")}]

    def normalize(self, items: list[dict]) -> list[ExternalEvent]:
        events = []
        for item in items:
            value = item["value"]
            event = build_external_event(
                text=f"Fear & Greed index {value} ({item['classification']})",
                source=self.name, source_type=SourceType.SENTIMENT,
                reliability=self.score_reliability(), timestamp=datetime.now(UTC),
                category=EventCategory.SENTIMENT, assets=[],
            )
            event.sentiment = max(-1.0, min(1.0, (value - 50) / 50.0))
            events.append(event)
        return events


class ExternalPoller(BaseCollector):
    """Runs all configured providers on an interval, publishing normalized events."""

    name = "external_poller"
    source_type = SourceType.NEWS

    def __init__(self, providers: list[ExternalDataProvider], bus: EventBus,
                 *, poll_interval_s: int = 300) -> None:
        super().__init__()
        self.providers = providers
        self.bus = bus
        self.poll_interval_s = poll_interval_s

    async def run(self) -> None:
        self.health.healthy = True
        self.health.detail = f"{len(self.providers)} providers"
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_interval_s)

    async def poll_once(self) -> int:
        count = 0
        for provider in self.providers:
            try:
                items = await provider.fetch()
                for event in provider.normalize(items):
                    await self.bus.publish(Topics.EXTERNAL_EVENT, event)
                    self.health.mark_event()
                    count += 1
            except Exception:
                log.exception("provider %s failed", provider.name)
                self.health.parse_errors += 1
        return count

    def provider_health(self) -> dict[str, bool]:
        return {p.name: getattr(p, "_last_ok", False) for p in self.providers}
