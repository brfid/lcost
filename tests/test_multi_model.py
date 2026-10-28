"""Cross-provider model handling: non-Anthropic normalize, pricing, recalc.

These guard the multi-model refactor:
- Cline collector strips provider prefixes for OpenAI / Amazon / Meta.
- Pricing returns None (not a silent Sonnet fallback) for recognized but
  unpriced families (Nova, Llama, Mistral, bare GPT).
- recalc_ledger_costs preserves stored cost when pricing is None, and
  reports a `skipped` count in its return tuple.
- Display helpers recognize GPT-5 family and non-Anthropic families.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcost.collectors import _normalize_cline_model
from lcost.ledger import recalc_ledger_costs
from lcost.ops_data import model_color, short_model
from lcost.pricing import (
    calculate_cost,
    family_for_model,
    has_priced_model,
)


# ── _normalize_cline_model ────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("us.anthropic.claude-opus-4-7", "claude-opus-4-7"),
    ("global.anthropic.claude-sonnet-4-5", "claude-sonnet-4-5"),
    ("global.anthropic.claude-sonnet-4-5-1", "claude-sonnet-4-5-1"),
    ("anthropic.claude-opus-4", "claude-opus-4"),
    ("claude-opus-4-6", "claude-opus-4-6"),
    ("us.openai.gpt-5-5", "gpt-5-5"), ("openai.gpt-5-mini", "gpt-5-mini"),
    ("us.amazon.nova-pro-v1", "nova-pro-v1"),
    ("us.meta.llama-4-70b-instruct", "llama-4-70b-instruct"),
    ("eu.mistral.mistral-large-2", "mistral-large-2"),
    (None, None), ("", ""),
])
def test_normalize_provider_model(raw, expected):
    assert _normalize_cline_model(raw) == expected


# ── pricing.calculate_cost ────────────────────────────────────────────────

class TestPricing:
    def test_gpt5_priced(self):
        # 1M input tokens at $1.25/MTok
        cost = calculate_cost(1_000_000, 0, 0, 0, "gpt-5")
        assert cost == pytest.approx(1.25)

    def test_gpt5_5_has_its_own_verified_price(self):
        cost = calculate_cost(1_000, 0, 0, 0, "openai.gpt-5-5")
        assert cost == pytest.approx(0.0055)

    def test_gpt5_mini_priced(self):
        cost = calculate_cost(1_000_000, 1_000_000, 0, 0, "gpt-5-mini")
        # input 0.25 + output 2.00
        assert cost == pytest.approx(2.25)

    def test_codex_bedrock_luna_priced(self):
        cost = calculate_cost(
            1_000, 1_000, 1_000, 1_000,
            "openai.gpt-5.6-luna",
        )
        assert cost == pytest.approx(0.001837)
        fam = family_for_model("openai.gpt-5.6-luna")
        assert fam is not None and fam.key == "gpt-5.6-luna"

    def test_nova_unpriced_returns_none(self):
        # Family matches (nova) but rates are None — placeholder.
        assert calculate_cost(1000, 0, 0, 0, "nova-pro-v1") is None

    def test_llama_unpriced_returns_none(self):
        assert calculate_cost(1000, 0, 0, 0, "llama-4-70b") is None

    def test_has_priced_model(self):
        assert has_priced_model("gpt-5")
        assert has_priced_model("claude-opus-4-7")
        assert not has_priced_model("nova-pro-v1")
        assert not has_priced_model("llama-4-70b")

    def test_gpt5_family_specificity(self):
        """gpt-5-nano must not match the bare gpt-5 family (more general)."""
        fam = family_for_model("gpt-5-nano")
        assert fam is not None and fam.key == "gpt-5-nano"


# ── short_model display ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("claude-opus-4-6", "opus-4.6"), ("claude-opus-4-7", "opus-4.7"),
    ("claude-sonnet-4-5-20250929", "sonnet-4.5"),
    ("claude-haiku-4-5-20251001", "haiku-4.5"),
    ("gpt-5-5", "gpt-5.5"), ("gpt-5-nano", "gpt-5-nano"),
    ("gpt-5-mini", "gpt-5-mini"), ("nova-pro-v1", "nova"),
    # Context qualifiers must remain distinct; date/release suffixes must not.
    ("claude-opus-4-7:1m", "opus-4.7-1M"),
    ("claude-opus-4-7-1m", "opus-4.7-1M"),
    ("claude-opus-4-7-v1", "opus-4.7"),
    ("gpt-5:200k", "gpt-5-200K"), ("gpt-5-mini:200k", "gpt-5-mini-200K"),
    (None, "—"), ("", "—"), ("xyz-42", "xyz"),
])
def test_short_model(raw, expected):
    assert short_model(raw) == expected


# ── model_color ───────────────────────────────────────────────────────────

class TestModelColor:
    def test_gpt5_color_from_registry(self):
        # Whatever the registry color is, it must be derived (not #CC9966 fallback)
        from lcost.pricing import FAMILIES_BY_KEY
        expected = FAMILIES_BY_KEY["gpt-5"].color
        assert model_color(short_model("gpt-5")) == expected

    def test_unknown_family_falls_back(self):
        # An entirely unrecognized string should yield the neutral fallback
        color = model_color("xyz-42")
        assert color.startswith("#")


# ── recalc_ledger_costs None-skip ─────────────────────────────────────────

class TestRecalcSkipsUnpriced:
    def _ledger(self):
        return {
            # Priced entry — should recompute
            "cc:priced": {
                "source": "cc",
                "ts": "2026-01-01T00:00:00",
                "model": "claude-sonnet-4-5-20250929",
                "tokensIn": 1000, "tokensOut": 0,
                "cacheWrites": 0, "cacheReads": 0,
                "cost": 999.0,  # obviously wrong — recalc should replace
                "cacheSavings": 0.0,
            },
            # Unpriced entry — must be left alone
            "cline:unpriced": {
                "source": "cline",
                "ts": "2026-01-01T00:00:00",
                "model": "nova-pro-v1",
                "tokensIn": 1000, "tokensOut": 500,
                "cacheWrites": 0, "cacheReads": 0,
                "cost": 0.042,  # provider-reported; must survive
                "cacheSavings": 0.0,
            },
        }

    def test_dry_run_preserves_unpriced(self, tmp_path: Path):
        ledger = self._ledger()
        old, new, changed, skipped = recalc_ledger_costs(
            tmp_path / "ledger.json", ledger, dry_run=True,
        )
        assert skipped == 1
        assert changed == 1
        # Dry run: no mutation
        assert ledger["cline:unpriced"]["cost"] == 0.042
        assert ledger["cc:priced"]["cost"] == 999.0

    def test_write_preserves_unpriced(self, tmp_path: Path):
        ledger = self._ledger()
        ledger_path = tmp_path / "ledger.json"
        _, _, changed, skipped = recalc_ledger_costs(
            ledger_path, ledger, dry_run=False,
        )
        assert skipped == 1
        assert changed == 1
        # Unpriced preserved
        assert ledger["cline:unpriced"]["cost"] == 0.042
        # Priced recomputed (1000 tokens × $3/MTok = $0.003)
        assert ledger["cc:priced"]["cost"] == pytest.approx(0.003)
