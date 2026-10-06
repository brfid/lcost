"""Model pricing tables, family registry, and cost calculation.

Canonical registry of model families. Display layers (short-name, colors)
and pricing both read from the same list so adding a new family is a
one-line change here.

`get_model_pricing()` returns ``Optional[Dict]``. Three outcomes:

- `dict` — we have rates, callers can compute cost.
- `None` — family is recognized but rates are not configured (e.g. a
  placeholder for a model whose public pricing we haven't vetted).
- Default fallback pricing — only when the model string is entirely
  unrecognized.

The `None`-for-unpriced semantics matter for `recalc_ledger_costs`: we
must not silently reprice a GPT-class entry with Sonnet rates. Callers
check for `None` and leave the stored cost alone.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

# Bump this when any configured token price changes. New entries retain this
# reference so a future rate change is explainable without account billing.
RATE_CARD_VERSION = "2026-10-06"

# USD per million tokens
# Source: https://www.anthropic.com/pricing  (verify quarterly — update here if stale)
OPUS_PRICING = {
    'input': 5.00, 'output': 25.00, 'cache_write': 6.25, 'cache_read': 0.50,
}
SONNET_PRICING = {
    'input': 3.00, 'output': 15.00, 'cache_write': 3.75, 'cache_read': 0.30,
}
HAIKU_PRICING = {
    'input': 1.00, 'output': 5.00, 'cache_write': 1.25, 'cache_read': 0.10,
}

# Source: openai.com/pricing — published GPT-5 family rates (USD/MTok).
# These are base OpenAI direct rates. Bedrock GPT-5.6 and GPT-6 quotes select
# routing-specific rates below; older GPT families retain these legacy rates.
GPT5_PRICING = {
    'input': 1.25, 'output': 10.00, 'cache_write': 1.25, 'cache_read': 0.125,
}
GPT5_MINI_PRICING = {
    'input': 0.25, 'output': 2.00, 'cache_write': 0.25, 'cache_read': 0.025,
}
GPT5_NANO_PRICING = {
    'input': 0.05, 'output': 0.40, 'cache_write': 0.05, 'cache_read': 0.005,
}

# Codex Bedrock-backed token-rate estimates (USD/MTok). Cache writes are
# modeled at 1.25× uncached input for the GPT-5.6 family.
GPT56_SOL_PRICING = {
    'input': 4.00, 'output': 20.00, 'cache_write': 5.00, 'cache_read': 0.40,
}
GPT56_TERRA_PRICING = {
    'input': 2.00, 'output': 12.00, 'cache_write': 2.50, 'cache_read': 0.20,
}
GPT56_LUNA_PRICING = {
    'input': 0.20, 'output': 1.20, 'cache_write': 0.25, 'cache_read': 0.02,
}

# AWS Standard Global CRIS prices, verified 2026-10-06 against the model cards
# linked below. Mantle and US geographic inference include a 10% premium.
GPT6_LUNA_PRICING = {
    'input': 0.10, 'output': 0.50, 'cache_write': 0.125, 'cache_read': 0.01,
}
GPT6_SOL_PRICING = {
    'input': 2.00, 'output': 10.00, 'cache_write': 2.50, 'cache_read': 0.20,
}
GPT6_ASTRA_PRICING = {
    'input': 10.00, 'output': 50.00, 'cache_write': 12.50, 'cache_read': 1.00,
}
GPT61_SOL_PRICING = {
    'input': 2.00, 'output': 10.00, 'cache_write': 2.50, 'cache_read': 0.10,
}

_AWS_CARD_ROOT = "https://docs.aws.amazon.com/bedrock/latest/userguide/"
BEDROCK_MODEL_CARDS = {
    "gpt-5.6-luna": _AWS_CARD_ROOT + "model-card-openai-gpt-56-luna.html",
    "gpt-5.6-terra": _AWS_CARD_ROOT + "model-card-openai-gpt-56-terra.html",
    "gpt-5.6-sol": _AWS_CARD_ROOT + "model-card-openai-gpt-56-sol.html",
    "gpt-6-luna": _AWS_CARD_ROOT + "model-card-openai-gpt-6-luna.html",
    "gpt-6-sol": _AWS_CARD_ROOT + "model-card-openai-gpt-6-sol.html",
    "gpt-6-astra": _AWS_CARD_ROOT + "model-card-openai-gpt-6-astra.html",
    "gpt-6.1-sol": _AWS_CARD_ROOT + "model-card-openai-gpt-6-1-sol.html",
}
LONG_CONTEXT_THRESHOLD = 272_000
_GPT6_KEYS = frozenset(("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-6.1-sol"))
_MODEL_ALIASES = {
    "gpt-6-1-sol": "gpt-6.1-sol",
    **{f"gpt-5-6-{name}": f"gpt-5.6-{name}" for name in ("sol", "terra", "luna")},
}


def canonical_model_id(model: Optional[str]) -> str:
    """Normalize documented IDs without discarding routing in stored records."""
    lowered = (model or "").lower()
    lowered = re.sub(r"^(us|global|eu|apac|apne|apse)\.", "", lowered)
    lowered = re.sub(r"^(openai|anthropic|amazon|meta|mistral)\.", "", lowered)
    return _MODEL_ALIASES.get(lowered, lowered)


@dataclass(frozen=True)
class ModelFamily:
    """One model family: identifying tokens + display metadata + rates.

    `tokens` match against the normalized model ID; first family whose
    token appears wins. Keep more-specific tokens earlier in the registry.
    """
    key: str                      # short name used in display (e.g. "opus")
    tokens: tuple                 # substrings that identify this family
    color: str                    # hex color for OPS model-mix widget
    pricing: Optional[Dict[str, float]]  # None = family known, rates TBD


# Ordered — first match wins. Keep longer/more-specific tokens first.
FAMILIES: List[ModelFamily] = [
    # Anthropic
    ModelFamily("opus",   ("opus",),               "#FF9900", OPUS_PRICING),
    ModelFamily("sonnet", ("sonnet",),             "#9999CC", SONNET_PRICING),
    ModelFamily("haiku",  ("haiku",),              "#CC6699", HAIKU_PRICING),
    ModelFamily("gpt-6.1-sol", ("gpt-6.1-sol", "gpt-6-1-sol"),
                "#5ABF8B", GPT61_SOL_PRICING),
    ModelFamily("gpt-6-astra", ("gpt-6-astra",), "#B58AE0", GPT6_ASTRA_PRICING),
    ModelFamily("gpt-6-sol", ("gpt-6-sol",), "#43A97C", GPT6_SOL_PRICING),
    ModelFamily("gpt-6-luna", ("gpt-6-luna",), "#79BEDB", GPT6_LUNA_PRICING),
    # OpenAI (most specific first so gpt-5-nano doesn't match bare gpt-5)
    ModelFamily(
        "gpt-5.6-luna", ("gpt-5.6-luna", "gpt-5-6-luna"),
        "#59B4D9", GPT56_LUNA_PRICING,
    ),
    ModelFamily(
        "gpt-5.6-terra", ("gpt-5.6-terra", "gpt-5-6-terra"),
        "#3C9F6F", GPT56_TERRA_PRICING,
    ),
    ModelFamily(
        "gpt-5.6-sol", ("gpt-5.6-sol", "gpt-5-6-sol"),
        "#2B7A45", GPT56_SOL_PRICING,
    ),
    ModelFamily("gpt-5-nano", ("gpt-5-nano",),     "#66CC99", GPT5_NANO_PRICING),
    ModelFamily("gpt-5-mini", ("gpt-5-mini",),     "#66CCCC", GPT5_MINI_PRICING),
    ModelFamily("gpt-5",  ("gpt-5", "gpt5"),       "#33AA66", GPT5_PRICING),
    ModelFamily("gpt-4",  ("gpt-4", "gpt4"),       "#2E8B57", None),
    ModelFamily("gpt",    ("gpt",),                "#50B080", None),
    # Amazon Bedrock (Nova)
    ModelFamily("nova",   ("nova",),               "#FFCC33", None),
    # Google
    ModelFamily("gemini", ("gemini",),             "#4285F4", None),
    # Meta
    ModelFamily("llama",  ("llama",),              "#6A5ACD", None),
    # Mistral
    ModelFamily("mistral", ("mistral",),           "#FF7F50", None),
]

# Fast lookup: family key → ModelFamily. Used by display helpers.
FAMILIES_BY_KEY: Dict[str, ModelFamily] = {f.key: f for f in FAMILIES}

# Exact model-ID overrides. Use when a specific version diverges from the
# family default or when you want to pin pricing even if the ID contains
# a misleading substring.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic exact IDs
    'claude-opus-4-6': OPUS_PRICING,
    'claude-opus-4-7': OPUS_PRICING,
    'claude-sonnet-4-5-20250929': SONNET_PRICING,
    'claude-sonnet-4-6': SONNET_PRICING,
    'claude-haiku-4-5-20251001': HAIKU_PRICING,
}

# Only used when a model string is completely unrecognizable (no family
# match and no exact-ID hit). Kept as Sonnet so legacy un-attributed
# Anthropic entries don't misprice wildly. Explicit fallback, not silent.
DEFAULT_PRICING = SONNET_PRICING


def family_for_model(model: Optional[str]) -> Optional[ModelFamily]:
    """Find the ModelFamily whose tokens match ``model``, or None."""
    if not model:
        return None
    lowered = model.lower()
    canonical = canonical_model_id(model)
    if canonical in _GPT6_KEYS:
        return FAMILIES_BY_KEY[canonical]
    for fam in FAMILIES:
        # An unknown suffix/version must not inherit a verified GPT-6 price.
        if fam.key in _GPT6_KEYS:
            continue
        if any(tok in lowered for tok in fam.tokens):
            return fam
    return None


def get_model_pricing(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Look up pricing for a model.

    Returns:
      - Exact pricing dict when the model ID is in ``MODEL_PRICING`` or
        matches a family with configured rates.
      - ``None`` when the family is recognized but rates aren't set
        (placeholder entries). Callers must treat this as "do not
        compute / do not overwrite existing cost".
      - ``DEFAULT_PRICING`` only when no family matches at all.
    """
    if not model:
        return DEFAULT_PRICING
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    fam = family_for_model(model)
    if fam is None:
        return DEFAULT_PRICING
    return fam.pricing  # may be None


def has_priced_model(model: Optional[str]) -> bool:
    """True iff we can compute a real cost for this model."""
    return get_model_pricing(model) is not None


def pricing_reference_for_model(model: Optional[str], **context) -> Optional[str]:
    """Return a stable reference for the configured token rate used.

    ``None`` means the model family is known but deliberately unpriced.
    Completely unknown legacy models retain the explicit default reference.
    """
    quote = quote_pricing(model, **context)
    return quote.reference if quote is not None else None


@dataclass(frozen=True)
class PriceQuote:
    rates: Dict[str, float]
    reference: str
    source: Optional[str] = None
    route: Optional[str] = None
    tier: Optional[str] = None
    context: Optional[str] = None
    assumptions: Tuple[str, ...] = ()


def quote_pricing(model: Optional[str], *, provider: Optional[str] = None,
                  inference_route: Optional[str] = None,
                  service_tier: Optional[str] = None,
                  input_tokens: Optional[int] = None,
                  assume_route: Optional[str] = None) -> Optional[PriceQuote]:
    """Select a verified price, retaining assumptions for an incomplete record."""
    rates = get_model_pricing(model)
    if rates is None:
        return None
    family = family_for_model(model)
    key = family.key if family is not None else "default"
    canonical = canonical_model_id(model)
    raw = (model or "").lower()
    bedrock = ("bedrock" in (provider or "").lower()
               or raw.startswith(("us.openai.", "global.openai.")))
    contextual = canonical in _GPT6_KEYS or (
        bedrock and canonical in BEDROCK_MODEL_CARDS
    )
    if not contextual:
        return PriceQuote(dict(rates), f"rate-card:{RATE_CARD_VERSION}:{key}")

    assumptions = []
    if not provider and not bedrock:
        assumptions.append("Provider not recorded; using OpenAI Standard base rates.")
    route = inference_route
    if route is None and raw.startswith("global.openai."):
        route = "global"
    elif route is None and raw.startswith("us.openai."):
        route = "us"
    if bedrock and route is None:
        route = assume_route or "regional-or-us"
        assumptions.append(f"Bedrock route not recorded; assumed {route}.")
    elif not bedrock:
        if route is not None:
            return None
        route = "direct"
    if route not in ("direct", "global", "us", "regional", "regional-or-us"):
        return None

    tier = (service_tier or "standard").lower()
    if tier == "default":
        tier = "standard"
    if tier not in ("standard", "ultrafast"):
        return None
    if tier == "ultrafast" and canonical != "gpt-6-astra":
        return None
    if service_tier is None:
        assumptions.append("Service tier not recorded; assumed Standard.")
    if input_tokens is None:
        return None
    context = "long" if input_tokens > LONG_CONTEXT_THRESHOLD else "short"
    multiplier = 1.1 if route in ("us", "regional", "regional-or-us") else 1.0
    if tier == "ultrafast":
        multiplier *= 6
    selected = {
        bucket: price * multiplier * (
            1.5 if bucket == "output" else 2
        ) if context == "long" else price * multiplier
        for bucket, price in rates.items()
    }
    reference = f"rate-card:{RATE_CARD_VERSION}:{canonical}:{route}:{tier}:{context}"
    return PriceQuote(selected, reference, BEDROCK_MODEL_CARDS[canonical],
                      route, tier, context, tuple(assumptions))


def pricing_context_for_entry(entry: Mapping) -> Dict:
    """Use the recorded request input, including both cache buckets."""
    input_tokens = entry.get("reportedInputTokens")
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        values = [entry.get(field) for field in ("tokensIn", "cacheWrites", "cacheReads")]
        input_tokens = sum(values) if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values
        ) else None
    return {
        "provider": entry.get("modelProvider"),
        "inference_route": entry.get("inferenceRoute"),
        "service_tier": entry.get("serviceTier"),
        "input_tokens": input_tokens,
    }


def quote_for_entry(entry: Mapping, *, assume_route: Optional[str] = None
                    ) -> Optional[PriceQuote]:
    model = entry.get("rawModel") or entry.get("model")
    if not isinstance(model, str) or not model:
        return None
    return quote_pricing(model, assume_route=assume_route,
                         **pricing_context_for_entry(entry))


def quote_metadata(quote: PriceQuote) -> Dict:
    metadata = {"pricingRef": quote.reference}
    if quote.source is not None:
        metadata.update({
            "pricingSource": quote.source, "pricingVerifiedAt": RATE_CARD_VERSION,
            "pricingRoute": quote.route, "pricingTier": quote.tier,
            "pricingContext": quote.context,
            "pricingAssumptions": list(quote.assumptions),
        })
    return metadata


def estimate_entry(entry: Mapping, *, assume_route: Optional[str] = None
                   ) -> Optional[Dict]:
    """Calculate complete usage buckets without charging reasoning twice."""
    values = [entry.get(field) for field in
              ("tokensIn", "tokensOut", "cacheWrites", "cacheReads")]
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in values):
        return None
    quote = quote_for_entry(entry, assume_route=assume_route)
    if quote is None:
        return None
    ti, to, cw, cr = values
    rates = quote.rates
    return {
        "cost": (ti * rates["input"] + to * rates["output"]
                 + cw * rates["cache_write"] + cr * rates["cache_read"]) / 1_000_000,
        "cacheSavings": cr * (rates["input"] - rates["cache_read"]) / 1_000_000,
        "costProvenance": "rate_card_estimate",
        **quote_metadata(quote),
    }


def calculate_cost(tokens_in: int, tokens_out: int, cache_writes: int,
                   cache_reads: int, model: Optional[str] = None, **context
                   ) -> Optional[float]:
    """Calculate USD cost from token counts. ``None`` when rates are absent."""
    context.setdefault("input_tokens", tokens_in + cache_writes + cache_reads)
    quote = quote_pricing(model, **context)
    if quote is None:
        return None
    pricing = quote.rates
    return (
        tokens_in * pricing['input']
        + tokens_out * pricing['output']
        + cache_writes * pricing['cache_write']
        + cache_reads * pricing['cache_read']
    ) / 1_000_000


def calculate_cache_savings(cache_reads: int,
                            model: Optional[str] = None, **context) -> float:
    """Calculate cost savings from prompt caching. 0.0 when rates absent."""
    if cache_reads == 0:
        return 0.0
    context.setdefault("input_tokens", cache_reads)
    quote = quote_pricing(model, **context)
    if quote is None:
        return 0.0
    pricing = quote.rates
    savings_per_million = pricing['input'] - pricing['cache_read']
    return (cache_reads * savings_per_million) / 1_000_000
