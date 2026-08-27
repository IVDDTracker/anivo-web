"""Stable content hashing for event deduplication and idempotency keys."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_WS_RE = re.compile(r"\s+")


def stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(source: str, *parts: Any) -> str:
    """sha256 over source + stable content parts."""
    h = hashlib.sha256()
    h.update(source.encode())
    for part in parts:
        h.update(b"\x1f")
        h.update(stable_json(part).encode())
    return h.hexdigest()


def normalized_text(text: str) -> str:
    """Lowercased, whitespace-collapsed, punctuation-stripped text for fuzzy clustering."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def headline_cluster_key(assets: list[str], category: str, headline: str) -> str:
    """COARSE cluster key for cross-source confirmation: (assets, category).

    Same-story membership within a coarse cluster is decided by word-set
    similarity (`story_similarity`), so differently-worded reports of one event
    cluster together while the headline itself doesn't fragment the key.
    """
    del headline  # intentionally not part of the key — see docstring
    h = hashlib.sha256()
    h.update(",".join(sorted(assets)).encode())
    h.update(b"|")
    h.update(category.encode())
    return h.hexdigest()[:32]


def significant_words(text: str) -> frozenset[str]:
    return frozenset(w for w in normalized_text(text).split() if len(w) > 3)


def story_similarity(a: str, b: str) -> float:
    """Jaccard similarity of significant word sets (0..1)."""
    wa, wb = significant_words(a), significant_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def deterministic_client_order_id(intent_id: str) -> str:
    """Deterministic Binance clientOrderId from a persisted intent id.

    Re-submitting the same intent reuses the same id, so the exchange rejects an
    accidental duplicate instead of double-executing (see DECISIONS.md D-013).
    """
    digest = hashlib.sha256(intent_id.encode()).hexdigest()[:24]
    return f"ql-{digest}"
