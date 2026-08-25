"""Tests for the small canonical usage-record boundary."""

from lcost.ledger import ingest, recalc_ledger_costs
from lcost.pricing import RATE_CARD_VERSION
from lcost.formatters import format_cost
from lcost.usage import (
    COST_PROVENANCE_RATE_CARD,
    COST_PROVENANCE_SOURCE_REPORTED,
    normalize_usage_entry,
)


def test_codex_rate_card_metadata_is_added_at_ingest_boundary():
    entry = normalize_usage_entry({
        "source": "codex",
        "model": "openai.gpt-5.6-luna",
        "tokensIn": 100,
        "tokensOut": 25,
        "cacheWrites": 5,
        "cacheReads": 50,
        "cost": 0.01,
        "cacheSavings": 0.001,
    })

    assert entry["costProvenance"] == COST_PROVENANCE_RATE_CARD
    assert entry["pricingRef"] == (
        f"rate-card:{RATE_CARD_VERSION}:gpt-5.6-luna"
    )


def test_cline_cost_is_marked_source_reported_without_billing_modes():
    entry = normalize_usage_entry({
        "source": "cline",
        "tokensIn": 100,
        "tokensOut": 25,
        "cost": 0.01,
    })

    assert entry["costProvenance"] == COST_PROVENANCE_SOURCE_REPORTED
    assert entry["pricingRef"] == "cline:source-reported"
    assert "billingMode" not in entry


def test_invalid_numeric_observations_are_unknown_not_zero():
    entry = normalize_usage_entry({
        "source": "codex",
        "tokensIn": float("nan"),
        "tokensOut": "not-a-number",
        "cacheWrites": -8,
        "cacheReads": 2.9,
        "cost": float("inf"),
        "cacheSavings": "bad",
    })

    assert entry["tokensIn"] is None
    assert entry["tokensOut"] is None
    assert entry["cacheWrites"] == 0
    assert entry["cacheReads"] == 2
    assert entry["cost"] is None
    assert entry["cacheSavings"] is None


def test_ledger_ingest_normalizes_collector_records():
    ledger = {}

    ingest(ledger, {
        "cc:one": {
            "source": "cc",
            "model": "claude-sonnet-4-6",
            "tokensIn": 10,
            "tokensOut": 2,
            "cost": 0.0001,
        },
    })

    stored = ledger["cc:one"]
    assert stored["costProvenance"] == COST_PROVENANCE_RATE_CARD
    assert stored["pricingRef"] == f"rate-card:{RATE_CARD_VERSION}:sonnet"
    assert stored["cacheReads"] is None


def test_subcent_query_price_is_not_rendered_as_zero():
    assert format_cost(0.005) == "$0.0050"


def test_recalc_preserves_cost_when_token_accounting_is_incomplete(tmp_path):
    ledger = {
        "codex:partial": {
            "source": "codex",
            "model": "openai.gpt-5.6-luna",
            "tokensIn": 100,
            "tokensOut": 10,
            "cacheWrites": None,
            "cacheReads": None,
            "cost": 0.12,
            "cacheSavings": None,
        },
    }

    _, _, changed, skipped = recalc_ledger_costs(
        tmp_path / "ledger.json", ledger, dry_run=True,
    )

    assert changed == 0
    assert skipped == 1
    assert ledger["codex:partial"]["cost"] == 0.12
