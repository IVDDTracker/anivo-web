"""Event intelligence: dedup, syndication vs confirmation, telegram cap, decay."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.bus import EventBus
from app.data.intelligence import EventIntelligence
from app.data.normalization.events import build_external_event
from app.models.enums import EventCategory, SourceType
from app.storage.repositories import EventRepository


@pytest.fixture
def intel(db, sim_clock):
    return EventIntelligence(EventRepository(db), sim_clock, EventBus())


def hack_event(sim_clock, *, source, source_type=SourceType.NEWS, reliability=0.7,
               text="Exchange X hacked, funds drained from hot wallet", offset_min=0):
    return build_external_event(
        text=text, source=source, source_type=source_type, reliability=reliability,
        timestamp=sim_clock.now() + timedelta(minutes=offset_min))


async def test_exact_duplicate_dropped(intel, sim_clock):
    e1 = hack_event(sim_clock, source="rss:coindesk")
    assert await intel.process(e1) is not None
    dup = e1.model_copy(update={"id": "other-id"})
    assert await intel.process(dup) is None


async def test_syndicated_copies_count_once(intel, sim_clock):
    # same wording from three outlets = ONE origin, no confirmation boost
    first = await intel.process(hack_event(sim_clock, source="rss:coindesk"))
    copy2 = await intel.process(hack_event(sim_clock, source="rss:blockworks", offset_min=1))
    copy3 = await intel.process(hack_event(sim_clock, source="rss:decrypt", offset_min=2))
    assert first.confirmation_count == 0
    assert copy3.confirmation_count == 0  # identical headlines → syndication, not confirmation
    assert copy3.confidence <= first.confidence + 1e-6
    assert copy2.novelty < 1.0


async def test_independent_wording_confirms(intel, sim_clock):
    await intel.process(hack_event(sim_clock, source="rss:coindesk"))
    confirmed = await intel.process(hack_event(
        sim_clock, source="rss:official-exchange", reliability=0.95,
        text="Security incident update: Exchange X hot wallet drained, funds stolen",
        offset_min=5))
    assert confirmed.confirmation_count >= 1
    assert confirmed.confidence > 0.9  # 1-(1-0.7)(1-0.95)


async def test_telegram_only_cluster_capped(intel, sim_clock):
    await intel.process(hack_event(
        sim_clock, source="telegram:alpha", source_type=SourceType.TELEGRAM, reliability=0.35))
    second = await intel.process(hack_event(
        sim_clock, source="telegram:beta", source_type=SourceType.TELEGRAM, reliability=0.35,
        text="Exchange X drained!!! hot wallet hack confirmed by nobody", offset_min=3))
    assert second.confidence <= 0.35  # many telegrams still aren't a confirmation


async def test_telegram_plus_official_confirms(intel, sim_clock):
    await intel.process(hack_event(
        sim_clock, source="telegram:alpha", source_type=SourceType.TELEGRAM, reliability=0.35))
    confirmed = await intel.process(hack_event(
        sim_clock, source="rss:official-exchange", reliability=0.95,
        text="Official statement: Exchange X security incident, withdrawals paused",
        offset_min=10))
    assert confirmed.confidence > 0.9


async def test_evidence_score_sign_and_decay(intel, sim_clock):
    event = build_external_event(
        text="Bitcoin ETF approved — bullish adoption growth", source="rss:coindesk",
        source_type=SourceType.NEWS, reliability=0.7, timestamp=sim_clock.now(),
        category=EventCategory.REGULATORY)
    assert "BTCUSDT" in event.assets
    event.sentiment = 0.8
    await intel.process(event)
    fresh = await intel.evidence_score("BTCUSDT")
    assert fresh > 0
    sim_clock.advance_to(sim_clock.now() + timedelta(days=20))
    assert await intel.evidence_score("BTCUSDT") == 0.0


async def test_negative_event_negative_score(intel, sim_clock):
    event = hack_event(sim_clock, source="rss:coindesk", reliability=0.9,
                       text="Bitcoin bridge hacked, BTC dumped in selloff")
    await intel.process(event)
    assert await intel.evidence_score("BTCUSDT") < 0
