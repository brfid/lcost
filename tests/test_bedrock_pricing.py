"""Verified AWS rate examples and safe historical price migration."""

import copy
import json

import pytest

from lcost.aggregation import aggregate_by_day, format_output
from lcost.collectors import extract_cline_entries
from lcost.ledger import merge_entries, recalc_ledger_costs
from lcost.ops_data import short_model
from lcost.pricing import calculate_cost, estimate_entry, family_for_model


def usage(model="openai.gpt-6.1-sol", **changes):
    entry = {
        "source": "codex", "model": model, "modelProvider": "amazon-bedrock",
        "ts": "2026-10-06T11:00:00", "tokensIn": 10_000, "tokensOut": 1_000,
        "cacheWrites": 2_000, "cacheReads": 50_000,
        "reportedInputTokens": 62_000, "reasoningTokens": 400,
        "cost": None, "cacheSavings": 0.0,
    }
    entry.update(changes)
    return entry


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-luna", 0.002475), ("gpt-6-sol", 0.0495),
    ("gpt-6-astra", 0.2475), ("gpt-6.1-sol", 0.044),
])
@pytest.mark.parametrize("prefix", ["", "openai.", "us.openai."])
def test_standard_us_price_examples_and_labels(model, expected, prefix):
    entry = usage(prefix + model)
    estimate = estimate_entry(entry)
    assert estimate["cost"] == pytest.approx(expected)
    assert short_model(entry["model"]) == model
    assert estimate["pricingContext"] == "short"
    assert estimate["pricingTier"] == "standard"
    assert estimate["pricingSource"].startswith("https://docs.aws.amazon.com/")
    # Reasoning is already included in output, so changing its annotation
    # cannot increase the charge.
    assert estimate_entry({**entry, "reasoningTokens": 0})["cost"] == estimate["cost"]


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-luna", 0.00225), ("gpt-6-sol", 0.045),
    ("gpt-6-astra", 0.225), ("gpt-6.1-sol", 0.04),
])
def test_global_rates_have_no_extra_aws_premium(model, expected):
    estimate = estimate_entry(usage("global.openai." + model))
    assert estimate["cost"] == pytest.approx(expected)
    assert estimate["pricingRoute"] == "global"
    assert not any("route" in a for a in estimate["pricingAssumptions"])


@pytest.mark.parametrize("cached,expected,context", [
    (271_000, 0.04301, "short"), (271_001, 0.08052022, "long"),
])
def test_context_boundary_includes_cached_input(cached, expected, context):
    estimate = estimate_entry(usage(
        tokensIn=1_000, tokensOut=1_000, cacheWrites=0, cacheReads=cached,
        reportedInputTokens=1_000 + cached,
    ))
    assert estimate["cost"] == pytest.approx(expected)
    assert estimate["pricingContext"] == context


def test_missing_reported_input_is_reconstructed_from_complete_buckets():
    assert estimate_entry(usage(
        tokensIn=1_000, tokensOut=1_000, cacheWrites=0, cacheReads=271_001,
        reportedInputTokens=None,
    ))["cost"] == pytest.approx(0.08052022)
    assert estimate_entry(usage(cacheReads=None)) is None


def test_astra_ultrafast_is_six_times_standard_and_explicit():
    estimate = estimate_entry(usage("us.openai.gpt-6-astra", serviceTier="ultrafast"))
    assert estimate["cost"] == pytest.approx(1.485)
    assert estimate["pricingTier"] == "ultrafast"
    assert not any("tier" in a.lower() for a in estimate["pricingAssumptions"])
    sol = estimate_entry(usage(serviceTier="ultrafast"))
    assert sol["cost"] == pytest.approx(0.264)
    assert sol["pricingTier"] == "ultrafast"
    assert estimate_entry(usage(serviceTier="priority")) is None


@pytest.mark.parametrize("model", [
    "openai.gpt-6.2-sol", "openai.gpt-6-sol-unknown", "gpt-6-terra",
])
def test_future_gpt_models_remain_unpriced(model):
    assert family_for_model(model).key == "gpt"
    assert estimate_entry(usage(model)) is None


def test_hyphenated_alias_is_a_distinct_sol_61_model():
    entry = usage("us.openai.gpt-6-1-sol")
    assert short_model(entry["model"]) == "gpt-6.1-sol"
    assert estimate_entry(entry)["cost"] == pytest.approx(0.044)


def test_route_assumption_is_stored_but_cannot_override_recorded_route():
    assumed = estimate_entry(usage(), assume_route="global")
    assert assumed["cost"] == pytest.approx(0.04)
    assert "assumed global" in " ".join(assumed["pricingAssumptions"])
    observed = estimate_entry(usage("us.openai.gpt-6.1-sol"), assume_route="global")
    assert observed["cost"] == pytest.approx(0.044)
    assert observed["pricingRoute"] == "us"


def test_cline_retains_provider_and_route_and_preserves_reported_cost():
    records = [{
        "ts": 1791309600000, "say": "api_req_started",
        "modelInfo": {"modelId": "global.openai.gpt-6.1-sol", "providerId": "bedrock"},
        "text": json.dumps({
            "tokensIn": 10_000, "tokensOut": 1_000, "cacheWrites": 2_000,
            "cacheReads": 50_000, "cost": 7.0,
        }),
    }]
    entry = next(iter(extract_cline_entries(records, "task").values()))
    assert entry["model"] == "gpt-6.1-sol"
    assert entry["rawModel"] == "global.openai.gpt-6.1-sol"
    assert entry["modelProvider"] == "bedrock"
    assert entry["cost"] == 7.0
    assert entry["cacheSavings"] == pytest.approx(0.095)


def test_selected_unpriced_migration_is_dry_run_safe_and_repeatable(tmp_path):
    ledger = {
        "missing": usage(),
        "priced": usage(cost=8.0),
        "other-model": usage("openai.gpt-5.6-sol"),
        "other-provider": usage(modelProvider="openai"),
        "cline": usage(source="cline", cost=9.0, costProvenance="source_reported"),
    }
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    original = copy.deepcopy(ledger)
    original_bytes = path.read_bytes()
    selection = dict(
        source_filter="codex", model_filters=["gpt-6.1-sol"],
        provider_filter="amazon-bedrock", only_unpriced=True,
    )
    old, new, changed, skipped = recalc_ledger_costs(path, ledger, dry_run=True, **selection)
    assert (old, changed, skipped) == (0, 1, 0)
    assert new == pytest.approx(0.044)
    assert ledger == original
    assert path.read_bytes() == original_bytes
    assert not (tmp_path / "backups").exists()

    recalc_ledger_costs(path, ledger, **selection)
    assert ledger["missing"]["cost"] == pytest.approx(0.044)
    assert ledger["missing"]["costProvenance"] == "rate_card_estimate"
    for key in ("priced", "other-model", "other-provider", "cline"):
        assert ledger[key] == original[key]
    backups = list((tmp_path / "backups").glob("recalc-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original_bytes
    applied_bytes = path.read_bytes()
    assert recalc_ledger_costs(path, ledger, **selection)[2] == 0
    assert path.read_bytes() == applied_bytes


def test_source_reported_prices_require_explicit_replacement(tmp_path):
    ledger = {"entry": usage(source="cline", cost=7.0, costProvenance="source_reported")}
    assert recalc_ledger_costs(tmp_path / "ledger.json", ledger)[2] == 0
    assert ledger["entry"]["cost"] == 7.0
    assert recalc_ledger_costs(
        tmp_path / "ledger.json", ledger, include_source_reported=True,
    )[2] == 1
    assert ledger["entry"]["cost"] == pytest.approx(0.044)


def test_reparse_cannot_attach_a_new_rate_reference_to_an_old_cost():
    original = usage("openai.gpt-5.6-sol", cost=9.0, pricingRef="rate-card:old")
    ledger = {"entry": original.copy()}
    incoming = {**original, **estimate_entry(original)}
    merge_entries(ledger, {"entry": incoming})
    assert ledger["entry"]["cost"] == 9.0
    assert ledger["entry"]["pricingRef"] == "rate-card:old"
    assert "pricingRoute" not in ledger["entry"]


def test_reports_count_only_unpriced_queries_in_the_selected_period():
    ledger = {
        "missing": usage(), "priced": usage(cost=0.044),
        "old": usage(ts="2026-01-01T11:00:00"),
    }
    daily = aggregate_by_day(ledger, date_from="2026-10-06")
    output = format_output(daily)
    assert "PARTIAL PRICE: 1 unpriced requests (63,000 tokens)" in output
    assert daily["2026-10-06"]["cost"] == pytest.approx(0.044)
