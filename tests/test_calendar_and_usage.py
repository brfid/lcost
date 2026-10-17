"""Meaningful totals, calendar boundaries, and tolerant timestamp handling."""
from datetime import datetime, timedelta, timezone

import pytest

from lcost.aggregation import aggregate_by_day, entry_local_dt
from lcost.cli import _format_age
from lcost.formatters import total_tokens
from lcost.ingest_state import hours_since_last_ingest, timestamp_age
from lcost.live_metrics import ClockBucket, compute_snapshot
from lcost.ops_data import build_row_specs, collect_entries, hour_week_grid


def test_calendar_periods_exclude_old_activity_and_include_idle_days(dashboard_clock):
    now = dashboard_clock.now
    ledger = {}
    for age, cost in [(0, 7), (6, 14), (7, 28), (29, 56), (30, 112), (40, 224)]:
        ledger[str(age)] = {
            "source": "cline", "ts": (now - timedelta(days=age)).isoformat(),
            "cost": cost, "tokensIn": cost, "tokensOut": cost,
            "cacheReads": cost * 2, "cacheWrites": cost,
            "reasoningTokens": cost,  # subset of output, never additive
        }
    snap = compute_snapshot(ledger, aggregate_by_day(ledger), None, ClockBucket.capture(now))
    cost, tokens = snap.overview.cost, snap.overview.tokens
    assert cost.today == 7 and cost.month == 105
    assert sum(cost.spark_7d) == 21 and cost.burn_rate == 3
    assert len(cost.spark_7d) == 7 and len(cost.spark_30d) == 30
    assert cost.spark_7d.count(0) == 5
    assert tokens.month == 525 and tokens.burn_rate == 15
    assert snap.ops.stats["tokens"] == 35
    assert sum(snap.ops.hour_tokens) == 35
    assert snap.source_today[0].tokens == 35


def test_total_tokens_counts_each_bucket_once_across_reductions(dashboard_clock, dashboard_ledger):
    entry = dashboard_ledger["codex:cached"]
    assert total_tokens(entry) == 10_000
    snap = compute_snapshot(dashboard_ledger, aggregate_by_day(dashboard_ledger), None)
    assert snap.overview.tokens.today == 10_100
    assert snap.ops.stats["project_tokens"]["cached-project"] == 10_000
    assert sum(snap.ops.hour_tokens) == 10_100
    assert sum(map(sum, hour_week_grid(dashboard_ledger, None, "tokens"))) == 10_100
    assert total_tokens({"tokensIn": None, "tokensOut": 5}) == 5


@pytest.mark.parametrize("timestamp", [None, 123, {}, "bad", "2026-02-30T12:00:00"])
def test_invalid_timestamps_are_skipped_everywhere(timestamp):
    ledger = {"bad": {"ts": timestamp, "source": "codex", "cost": 9}, "bad-row": None}
    assert aggregate_by_day(ledger) == {}
    assert collect_entries(ledger, None) == []
    assert sum(map(sum, hour_week_grid(ledger, None, "cost"))) == 0
    assert timestamp_age(timestamp) is None


def test_offset_timestamps_keep_their_instant_and_scan_ages_are_safe():
    utc = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    for source in ("cc", "codex", "cline"):
        assert entry_local_dt({"source": source, "ts": "2026-09-12T14:00:00+02:00"}) == utc.astimezone().replace(tzinfo=None)
    assert timestamp_age("2026-09-12T13:00:00+02:00", utc) == 3600
    assert hours_since_last_ingest({"last_ingest_at": utc.isoformat()}) is not None
    assert _format_age(utc.isoformat()) != "unknown"


def test_synthetic_spawns_do_not_inflate_counts_or_displace_call_rows(dashboard_clock):
    ts = dashboard_clock.now.isoformat()
    ledger = {str(i): {"ts": ts, "source": "agent_spawn"} for i in range(105)}
    ledger["real"] = {"ts": ts, "source": "cline", "cost": 1}
    assert sum(day["requests"] for day in aggregate_by_day(ledger).values()) == 1
    assert sum(map(sum, hour_week_grid(ledger, None, "requests"))) == 1
    rows = build_row_specs(collect_entries(ledger, None), now=dashboard_clock.now)
    assert [row.entry_id for row in rows] == ["real"]
