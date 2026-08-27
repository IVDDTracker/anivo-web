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
    """Cluster key for cross-source confirmation: same story from many outlets clusters together."""
    words = sorted(set(normalized_text(headline).split()))
    significant = [w for w in words if len(w) > 3][:12]
    h = hashlib.sha256()
    h.update(",".join(sorted(assets)).encode())
    h.update(b"|")
    h.update(category.encode())
    h.update(b"|")
    h.update(" ".join(significant).encode())
    return h.hexdigest()[:32]


def deterministic_client_order_id(intent_id: str) -> str:
    """Deterministic Binance clientOrderId from a persisted intent id.

    Re-submitting the same intent reuses the same id, so the exchange rejects an
    accidental duplicate instead of double-executing (see DECISIONS.md D-013).
    """
    digest = hashlib.sha256(intent_id.encode()).hexdigest()[:24]
    return f"ql-{digest}"
