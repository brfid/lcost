"""Regressions for safe local usage ingestion shared by company users."""

import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from lcost.cli import main
from lcost.collectors import collect_claude_code_data, parse_claude_code_session
from lcost.ingest_state import (
    file_needs_processing,
    get_stored_user_text,
    load_ingest_state,
    new_ingest_state,
    save_ingest_state,
    update_file_state,
)
from lcost.ledger import save_ledger
from lcost.pidfile import single_instance


def _assistant_line(message_id):
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-10-28T12:00:00Z",
        "message": {
            "id": message_id,
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }) + "\n"


def test_report_writer_refuses_an_active_dashboard(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.json"
    save_ledger(path, {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("sys.argv", ["lcost", "--report", "--ledger-path", str(path)])
    scan = Mock()
    monkeypatch.setattr("lcost.cli.run_ingest", scan)

    with single_instance():
        assert main() == 1

    scan.assert_not_called()
    assert "--report --cached" in capsys.readouterr().out


def test_report_reloads_ledger_after_acquiring_writer_lock(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    save_ledger(path, {})
    latest = {"late": {"source": "cline", "ts": "2026-10-28T12:00:00", "cost": 1.0}}

    @contextmanager
    def acquire(**kwargs):
        save_ledger(path, latest)
        yield

    monkeypatch.setattr("lcost.pidfile.single_instance", acquire)
    monkeypatch.setattr("sys.argv", ["lcost", "--report", "--ledger-path", str(path)])
    scan = Mock(return_value=0)
    monkeypatch.setattr("lcost.cli.run_ingest", scan)

    assert main() == 0
    assert scan.call_args.args[1] == latest


def test_cached_report_can_read_while_dashboard_runs(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    save_ledger(path, {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("sys.argv", [
        "lcost", "--report", "--cached", "--ledger-path", str(path),
    ])

    with single_instance():
        assert main() == 0

    assert not (tmp_path / "ingest_state.json").exists()
    assert not (tmp_path / "backups").exists()


def test_append_between_parse_and_state_update_is_not_skipped(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    session = root / "projects" / "test" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(_assistant_line("first"))
    state = new_ingest_state()
    original_update = update_file_state
    updates = 0

    def append_before_stat(ingest_state, path, offset, last_user_text=""):
        nonlocal updates
        updates += 1
        if updates == 1:
            with path.open("a") as handle:
                handle.write(_assistant_line("second"))
        original_update(ingest_state, path, offset, last_user_text)

    monkeypatch.setattr("lcost.collectors.get_claude_code_dir", lambda: root)
    monkeypatch.setattr("lcost.collectors.update_file_state", append_before_stat)

    assert set(collect_claude_code_data(False, state)) == {"cc:first"}
    assert set(collect_claude_code_data(False, state)) == {"cc:second"}
    assert file_needs_processing(session, state) == (False, 0)


def test_only_preview_seed_is_kept_in_ingest_state(tmp_path):
    session = tmp_path / "session.jsonl"
    prompt = "synthetic private prompt " * 100
    session.write_text(
        json.dumps({"type": "user", "message": {"content": prompt}}) + "\n"
        + _assistant_line("first")
    )

    entries, offset, last_user_text = parse_claude_code_session(session)
    assert entries["cc:first"]["promptPreview"] == prompt[:80]
    assert last_user_text == prompt[:80]

    state = new_ingest_state()
    update_file_state(state, session, offset, prompt)
    assert state["files"][str(session)]["last_user_text"] == prompt[:80]

    # Existing state created by older versions also loses excess prompt text,
    # including files skipped by subsequent incremental scans.
    state["files"][str(session)]["last_user_text"] = prompt
    assert get_stored_user_text(state, session) == prompt[:80]
    state_path = tmp_path / "ingest_state.json"
    save_ingest_state(state_path, state)
    loaded = load_ingest_state(state_path)
    assert loaded["files"][str(session)]["last_user_text"] == prompt[:80]
    save_ingest_state(state_path, loaded)
    assert prompt not in state_path.read_text()


def _haiku_cached_record(tokens_in=10_000):
    return {
        "type": "assistant",
        "timestamp": "2026-10-28T12:00:00Z",
        "model_provider": "amazon-bedrock",
        "inference_route": "us",
        "message": {
            "id": "haiku",
            "model": "us.anthropic.claude-haiku-5-5",
            "stop_reason": "end_turn",
            "service_tier": "standard",
            "usage": {
                "input_tokens": tokens_in,
                "output_tokens": 1_000,
                "cache_creation_input_tokens": 60_000,
                "cache_read_input_tokens": 40_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10_000,
                    "ephemeral_1h_input_tokens": 50_000,
                },
            },
        },
    }


@pytest.mark.parametrize("tokens_in,context,cost,savings", [
    (0, "short", 0.013365, 0.00396),
    (10_000, "long", 0.072325, 0.0198),
])
def test_claude_cache_ttls_and_savings_use_whole_request_context(
    tmp_path, tokens_in, context, cost, savings,
):
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps(_haiku_cached_record(tokens_in)) + "\n")

    entries, _, _ = parse_claude_code_session(session)
    entry = entries["cc:haiku"]
    assert entry["cacheWrites5m"] == 10_000
    assert entry["cacheWrites1h"] == 50_000
    assert entry["rawModel"] == "us.anthropic.claude-haiku-5-5"
    assert entry["modelProvider"] == "amazon-bedrock"
    assert entry["inferenceRoute"] == "us"
    assert entry["serviceTier"] == "standard"
    assert entry["pricingContext"] == context
    assert entry["pricingRoute"] == "us"
    assert entry["cost"] == pytest.approx(cost)
    assert entry["cacheSavings"] == pytest.approx(savings)
    assert entry["costProvenance"] == "rate_card_estimate"


@pytest.mark.parametrize("split,expected", [
    ({"ephemeral_5m_input_tokens": 10_000}, (10_000, None)),
    ({"ephemeral_5m_input_tokens": 10_000, "ephemeral_1h_input_tokens": 40_000},
     (10_000, 40_000)),
    ({"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": 50_000},
     (None, 50_000)),
    ({"ephemeral_5m_input_tokens": 10_000.0, "ephemeral_1h_input_tokens": 50_000},
     (None, 50_000)),
    ({"ephemeral_5m_input_tokens": -1, "ephemeral_1h_input_tokens": "unknown"},
     (None, None)),
    (None, (None, None)),
])
def test_malformed_claude_cache_split_remains_unpriced(tmp_path, split, expected):
    session = tmp_path / "session.jsonl"
    record = _haiku_cached_record()
    record["message"]["usage"]["cache_creation"] = split
    session.write_text(json.dumps(record) + "\n")

    entries, _, _ = parse_claude_code_session(session)
    entry = entries["cc:haiku"]
    assert (entry["cacheWrites5m"], entry["cacheWrites1h"]) == expected
    assert entry["cacheWrites"] == 60_000
    assert entry["cost"] is None
    assert entry["cacheSavings"] is None
    assert "pricingRef" not in entry


def test_missing_claude_token_bucket_remains_unknown(tmp_path):
    session = tmp_path / "session.jsonl"
    record = _haiku_cached_record()
    del record["message"]["usage"]["cache_read_input_tokens"]
    session.write_text(json.dumps(record) + "\n")

    entries, _, _ = parse_claude_code_session(session)
    entry = entries["cc:haiku"]
    assert entry["cacheReads"] is None
    assert entry["cost"] is None


def test_absent_claude_cache_split_records_five_minute_assumption(tmp_path):
    session = tmp_path / "session.jsonl"
    record = _haiku_cached_record()
    del record["message"]["usage"]["cache_creation"]
    session.write_text(json.dumps(record) + "\n")

    entries, _, _ = parse_claude_code_session(session)
    entry = entries["cc:haiku"]
    assert "cacheWrites5m" not in entry
    assert "cacheWrites1h" not in entry
    assert entry["cost"] == pytest.approx(0.0517)
    assert "Cache-write TTL not recorded; assumed 5 minutes." in entry["pricingAssumptions"]


def test_claude_historical_provider_is_not_inferred_from_environment(
    tmp_path, monkeypatch,
):
    session = tmp_path / "session.jsonl"
    record = _haiku_cached_record()
    record["message"]["model"] = "claude-haiku-5-5"
    del record["model_provider"]
    del record["inference_route"]
    del record["message"]["service_tier"]
    session.write_text(json.dumps(record) + "\n")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    entries, _, _ = parse_claude_code_session(session)
    entry = entries["cc:haiku"]
    assert "modelProvider" not in entry
    assert "inferenceRoute" not in entry
    assert "serviceTier" not in entry
    assert entry["pricingRoute"] == "direct"
