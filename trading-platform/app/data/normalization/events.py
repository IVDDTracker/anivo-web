"""Rule-based classification of external text into ExternalEvent fields.

Shared by Telegram/RSS/GitHub collectors. Interpretable keyword rules; an
optional LLM can refine these fields later but the rule-based result always
exists (DECISIONS.md D-018).
"""

from __future__ import annotations

from datetime import datetime

from app.core.hashing import event_hash, headline_cluster_key, normalized_text
from app.models.enums import EventCategory, SourceType
from app.models.events import ExternalEvent

# category keywords: first match wins (ordered by severity/specificity)
_CATEGORY_KEYWORDS: list[tuple[EventCategory, tuple[str, ...]]] = [
    (EventCategory.HACK, ("hack", "hacked", "stolen funds", "drained", "breach")),
    (EventCategory.EXPLOIT, ("exploit", "vulnerability", "cve-", "attack vector", "bug bounty")),
    (EventCategory.SECURITY_ADVISORY, ("security advisory", "critical patch", "ghsa-")),
    (EventCategory.DELISTING, ("delist", "delisting", "remove trading pair", "cease trading")),
    (EventCategory.LISTING, ("will list", "lists ", "listing", "new trading pair", "launchpool")),
    (EventCategory.OUTAGE, ("outage", "halted", "downtime", "network stall", "chain halt",
                            "not producing blocks")),
    (EventCategory.REGULATORY, ("sec ", "cftc", "regulator", "lawsuit", "settlement", "etf",
                                "sanction", "mica", "enforcement")),
    (EventCategory.TOKEN_UNLOCK, ("unlock", "vesting", "cliff")),
    (EventCategory.PARTNERSHIP, ("partnership", "partners with", "integration with",
                                 "collaboration")),
    (EventCategory.WHALE, ("whale", "large transfer", "moved from wallet")),
    (EventCategory.RELEASE, ("release", "mainnet", "hard fork", "upgrade", "testnet launch")),
    (EventCategory.MACRO, ("fed ", "fomc", "cpi", "inflation", "rate cut", "rate hike",
                           "treasury")),
    (EventCategory.RUMOR, ("rumor", "unconfirmed", "allegedly", "sources say")),
]

# category → (default magnitude, default sentiment sign, decay half-life hours)
_CATEGORY_PROFILE: dict[EventCategory, tuple[float, float, float]] = {
    EventCategory.HACK: (0.9, -1.0, 24.0),
    EventCategory.EXPLOIT: (0.8, -1.0, 24.0),
    EventCategory.SECURITY_ADVISORY: (0.6, -0.5, 24.0),
    EventCategory.DELISTING: (0.7, -1.0, 24.0),
    EventCategory.LISTING: (0.6, 1.0, 12.0),
    EventCategory.OUTAGE: (0.7, -1.0, 12.0),
    EventCategory.REGULATORY: (0.6, -0.3, 72.0),
    EventCategory.TOKEN_UNLOCK: (0.4, -0.3, 48.0),
    EventCategory.PARTNERSHIP: (0.3, 0.5, 12.0),
    EventCategory.WHALE: (0.3, -0.1, 6.0),
    EventCategory.RELEASE: (0.4, 0.3, 24.0),
    EventCategory.MACRO: (0.5, 0.0, 24.0),
    EventCategory.RUMOR: (0.3, 0.0, 2.0),
    EventCategory.SENTIMENT: (0.2, 0.0, 6.0),
    EventCategory.DEVELOPMENT: (0.2, 0.0, 24.0),
    EventCategory.OTHER: (0.1, 0.0, 12.0),
}

_ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTCUSDT": ("btc", "bitcoin", "xbt"),
    "ETHUSDT": ("eth", "ethereum", "ether"),
    "SOLUSDT": ("sol", "solana"),
    "BNBUSDT": ("bnb", "binance coin", "bsc", "bnb chain"),
}

_POSITIVE_WORDS = ("surge", "rally", "bullish", "adoption", "approve", "approved", "record high",
                   "partnership", "growth", "gain")
_NEGATIVE_WORDS = ("crash", "dump", "bearish", "ban", "banned", "reject", "hack", "exploit",
                   "lawsuit", "outage", "halt", "selloff", "liquidation", "fear")


def classify_category(text: str) -> EventCategory:
    lowered = f" {normalized_text(text)} "
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw.strip() in lowered for kw in keywords):
            return category
    return EventCategory.OTHER


def extract_assets(text: str, *, restrict_to: list[str] | None = None) -> list[str]:
    lowered = f" {normalized_text(text)} "
    found = []
    for symbol, aliases in _ASSET_ALIASES.items():
        if restrict_to and symbol not in restrict_to:
            continue
        if any(f" {alias} " in lowered or f" {alias}s " in lowered for alias in aliases):
            found.append(symbol)
    return found


def naive_sentiment(text: str) -> float:
    lowered = normalized_text(text)
    pos = sum(1 for w in _POSITIVE_WORDS if w in lowered)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in lowered)
    if pos == neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / max(pos + neg, 1)))


def build_external_event(
    *,
    text: str,
    source: str,
    source_type: SourceType,
    reliability: float,
    timestamp: datetime,
    url: str = "",
    category: EventCategory | None = None,
    assets: list[str] | None = None,
    restrict_assets: list[str] | None = None,
) -> ExternalEvent:
    cat = category or classify_category(text)
    magnitude, sent_sign, half_life = _CATEGORY_PROFILE.get(cat, (0.1, 0.0, 12.0))
    sentiment = naive_sentiment(text)
    if sentiment == 0.0 and sent_sign != 0.0:
        sentiment = sent_sign * 0.5
    resolved_assets = assets if assets is not None else extract_assets(
        text, restrict_to=restrict_assets)
    headline = text.strip().split("\n", 1)[0][:500]
    # initial confidence == own-source reliability; the reliability engine raises it
    # only through independent confirmation (DATA_SOURCES.md)
    return ExternalEvent(
        assets=resolved_assets,
        category=cat,
        headline=headline,
        body_excerpt=text[:2000],
        url=url[:1000],
        timestamp=timestamp,
        source=source,
        source_type=source_type,
        reliability=reliability,
        sentiment=sentiment,
        magnitude=magnitude,
        confidence=min(reliability, 0.95),
        decay_half_life_hours=half_life,
        event_hash=event_hash(source, "ext", normalized_text(headline), timestamp.isoformat()),
        cluster_key=headline_cluster_key(resolved_assets, cat.value, headline),
    )
