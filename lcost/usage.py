"""Small canonical contract for parsed query-usage records.

Collectors intentionally remain source-specific parsers. This module is the
single boundary between those parsers and the durable ledger: it validates
numeric observations, fills explicit unknowns, and records how a displayed
query price was obtained. It deliberately does not model subscriptions,
account balances, or invoices.
"""

import math
from typing import Dict, Mapping, Optional, TypedDict

from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
)
from .pricing import pricing_reference_for_model


COST_PROVENANCE_RATE_CARD = "rate_card_estimate"
COST_PROVENANCE_SOURCE_REPORTED = "source_reported"


class UsageEntry(TypedDict, total=False):
    """Fields shared by usage records from all supported local sources."""

    source: str
    ts: str
    model: Optional[str]
    project: str
    session: str
    tokensIn: Optional[int]
    tokensOut: Optional[int]
    cacheWrites: Optional[int]
    cacheReads: Optional[int]
    cost: Optional[float]
    cacheSavings: Optional[float]
    costProvenance: str
    pricingRef: str


_TOKEN_FIELDS = (
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    FIELD_CACHE_WRITES,
    FIELD_CACHE_READS,
)
_MONEY_FIELDS = (FIELD_COST, FIELD_CACHE_SAVINGS)


def normalize_usage_entry(entry: Mapping[str, object]) -> Dict[str, object]:
    """Return a ledger-ready copy with explicit numeric unknowns.

    Invalid and non-finite source values become ``None`` rather than a
    misleading zero. Valid token values are stored as non-negative integers;
    money values retain their supplied precision.
    """
    normalized: Dict[str, object] = dict(entry)
    for field in _TOKEN_FIELDS:
        normalized[field] = _token_value(normalized.get(field))
    for field in _MONEY_FIELDS:
        normalized[field] = _money_value(normalized.get(field))

    source = normalized.get("source")
    source_name = source if isinstance(source, str) else ""
    cost = normalized.get(FIELD_COST)
    if cost is None:
        return normalized

    if source_name == "cline":
        normalized.setdefault("costProvenance", COST_PROVENANCE_SOURCE_REPORTED)
        normalized.setdefault("pricingRef", "cline:source-reported")
    elif source_name in ("cc", "codex"):
        normalized.setdefault("costProvenance", COST_PROVENANCE_RATE_CARD)
        model = normalized.get("model")
        pricing_ref = pricing_reference_for_model(
            model if isinstance(model, str) else None
        )
        if pricing_ref is not None:
            normalized.setdefault("pricingRef", pricing_ref)
    return normalized


def normalize_usage_entries(entries: Mapping[str, Mapping[str, object]]
                            ) -> Dict[str, Dict[str, object]]:
    """Normalize a collector result without mutating collector-owned values."""
    return {
        entry_id: normalize_usage_entry(entry)
        for entry_id, entry in entries.items()
    }


def _token_value(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0, int(value))


def _money_value(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)
