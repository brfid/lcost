"""Current published rate examples and protection against invented prices."""

import pytest

from lcost.pricing import (
    calculate_cost, estimate_entry, family_for_model, get_model_pricing,
)
from tests.test_bedrock_pricing import usage


@pytest.mark.parametrize("model,base", [
    ("claude-haiku-4-5-20251001-v1:0", 0.00735),
    ("claude-sonnet-4-5-20250929-v1:0", 0.02205),
    ("claude-haiku-5-5", 0.000735),
    ("claude-sonnet-4-6", 0.02205),
    ("claude-sonnet-5", 0.0147),
    ("claude-sonnet-5-5", 0.0146),
    ("claude-opus-4-6-v1", 0.03675),
    ("claude-opus-4-7", 0.03675),
    ("claude-opus-4-8", 0.03675),
    ("claude-opus-5-5", 0.0292),
])
@pytest.mark.parametrize("route,premium", [("global", 1.0), ("us", 1.1)])
def test_claude_version_specific_standard_prices(model, base, route, premium):
    entry = usage(
        f"{route}.anthropic.{model}", tokensIn=1_000, tokensOut=1_000,
        cacheWrites=1_000, cacheReads=1_000, reportedInputTokens=3_000,
    )
    quote = estimate_entry(entry)
    assert quote["cost"] == pytest.approx(base * premium)
    assert quote["pricingRoute"] == route
    assert quote["pricingVerifiedAt"] == "2026-10-28"


@pytest.mark.parametrize("input_total,multiplier,context", [
    (100_000, 1, "short"), (100_001, 5, "long"),
])
def test_haiku_55_context_boundary_uses_all_cached_input(input_total, multiplier, context):
    entry = usage(
        "global.anthropic.claude-haiku-5-5", tokensIn=1_000, tokensOut=1_000,
        cacheWrites=1_000, cacheReads=input_total - 2_000,
        reportedInputTokens=None,
    )
    result = estimate_entry(entry)
    expected = (100 + 500 + 125 + (input_total - 2_000) * 0.01) / 1_000_000
    assert result["cost"] == pytest.approx(expected * multiplier)
    assert result["pricingContext"] == context
    assert result["cacheSavings"] == pytest.approx(
        entry["cacheReads"] * 0.09 * multiplier / 1_000_000,
    )


def test_other_claude_models_do_not_inherit_haiku_long_context_premium():
    result = estimate_entry(usage(
        "global.anthropic.claude-sonnet-5-5", tokensIn=400_000, tokensOut=0,
        cacheWrites=0, cacheReads=0, reportedInputTokens=400_000,
    ))
    assert result["cost"] == pytest.approx(0.8)
    assert result["pricingContext"] == "short"


def test_one_hour_and_five_minute_cache_writes_have_separate_rates():
    entry = usage(
        "us.anthropic.claude-sonnet-5-5", tokensIn=1_000, tokensOut=1_000,
        cacheWrites=3_000, cacheReads=1_000, reportedInputTokens=5_000,
        cacheWrites5m=1_000, cacheWrites1h=2_000,
    )
    result = estimate_entry(entry)
    assert result["cost"] == pytest.approx(0.02486)
    assert result["pricingRef"].endswith(":cache-5m-1h")
    assert not any("TTL" in text for text in result["pricingAssumptions"])
    del entry["cacheWrites5m"]
    del entry["cacheWrites1h"]
    result = estimate_entry(entry)
    assert result["cost"] == pytest.approx(0.02156)
    assert "assumed 5 minutes" in " ".join(result["pricingAssumptions"])


@pytest.mark.parametrize("changes", [
    {"cacheWrites5m": None, "cacheWrites1h": None},
    {"cacheWrites5m": 100, "cacheWrites1h": 100},
    {"cacheWrites5m": True, "cacheWrites1h": 1_999},
    {"cacheWrites5m": 2_000},
])
def test_invalid_cache_write_splits_remain_unpriced(changes):
    assert estimate_entry(usage("us.anthropic.claude-sonnet-5-5", **changes)) is None


@pytest.mark.parametrize("model,input_rate,output_rate,read_rate", [
    ("gpt-5.4", 2.75, 16.5, 0.275),
    ("gpt-5.5", 5.5, 33, 0.55),
])
def test_older_frontier_gpt_models_do_not_use_gpt5_rates(model, input_rate, output_rate, read_rate):
    result = estimate_entry(usage(
        "openai." + model, tokensIn=1_000, tokensOut=1_000,
        cacheWrites=0, cacheReads=1_000, reportedInputTokens=2_000,
        inferenceRoute="regional",
    ))
    assert result["cost"] == pytest.approx(
        (input_rate + output_rate + read_rate) / 1_000,
    )
    assert estimate_entry(usage("openai." + model, inferenceRoute="regional")) is None


@pytest.mark.parametrize("route", ["us", "global", "direct"])
@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.5"])
def test_unverified_gpt54_55_routes_remain_unpriced(model, route):
    assert estimate_entry(usage(
        "openai." + model, cacheWrites=0, reportedInputTokens=60_000,
        inferenceRoute=route,
    )) is None


@pytest.mark.parametrize("model", [
    None, "", "future-vendor.model", "claude-opus-99-99",
    "claude-haiku-future", "gpt-5.7", "gpt-5.6-sol-next",
    "claude-opus-4-7-v999:999", "claude-opus-4-7-20991231",
    "openai.gpt-oss-120b-1:0", "google.gemma-4-31b",
    "google.gemma-3-27b-it", "amazon.nova-2-lite-v1:0",
])
def test_unverified_model_prices_never_fall_back_to_another_model(model):
    assert get_model_pricing(model) is None
    assert calculate_cost(1_000, 1_000, 0, 0, model) is None


def test_gemma_and_gemini_are_separate_display_families():
    assert family_for_model("google.gemma-4-31b").key == "gemma"
    assert family_for_model("google.gemini-pro").key == "gemini"


def test_inconsistent_reported_input_cannot_bypass_long_context_rates():
    assert estimate_entry(usage(
        cacheReads=300_000, reportedInputTokens=1,
    )) is None


@pytest.mark.parametrize("changes", [
    {"inferenceRoute": "global"},
    {"inferenceRoute": "direct"},
    {"modelProvider": "openai"},
    {"modelProvider": "unknown-gateway"},
    {"serviceTier": "priority"},
])
def test_conflicting_or_unverified_pricing_metadata_is_not_guessed(changes):
    assert estimate_entry(usage("us.anthropic.claude-sonnet-5-5", **changes)) is None
