"""Extract candidate coin symbols from tweet text (spec §4).

Rules:
- `$TAO` and `#TAO` are explicit candidates (case-insensitive, 2-6 alnum chars);
- BARE uppercase tokens (`TAO`) count ONLY if they exist in the known-symbols
  whitelist (Binance futures base assets) — random words are never coins;
- common English words / trader jargon are excluded even if a coin shares the
  name, unless written as an explicit $/# tag ("LONG" the word vs "$LONG");
- final tradability is decided by the SymbolMapper against live exchangeInfo.
"""

from __future__ import annotations

import re

_TAGGED_RE = re.compile(r"[$#]([A-Za-z0-9]{2,10})\b")
_BARE_RE = re.compile(r"\b([A-Z0-9]{2,6})\b")

# words that are never treated as bare coin candidates (jargon & common words)
_STOPWORDS = {
    "LONG", "SHORT", "USD", "USDT", "USDC", "TP", "SL", "PNL", "ATH", "ATL", "DCA",
    "OK", "IMO", "FOMO", "REKT", "GM", "GN", "RT", "CEO", "AI", "API", "ETF", "FED",
    "NOW", "BUY", "SELL", "HOLD", "PUMP", "DUMP", "TA", "HTF", "LTF", "RSI", "EMA",
    "VWAP", "NEW", "BIG", "TOP", "DIP", "LFG", "WEN", "SOON", "NFA", "DYOR",
}


def extract_candidates(text: str, known_bases: set[str] | None = None) -> list[str]:
    """Ordered, de-duplicated candidate base symbols (upper-case, no $/#)."""
    seen: dict[str, None] = {}

    for match in _TAGGED_RE.finditer(text):
        token = match.group(1).upper()
        if 2 <= len(token) <= 10 and not token.isdigit():
            seen.setdefault(token, None)

    if known_bases:
        for match in _BARE_RE.finditer(text):
            token = match.group(1)
            if token in _STOPWORDS or token.isdigit():
                continue
            if token in known_bases:
                seen.setdefault(token, None)

    return list(seen)
