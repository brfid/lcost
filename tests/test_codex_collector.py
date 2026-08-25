"""Codex rollout ingestion, including the VS Code extension surface."""

import json
from pathlib import Path

import pytest

from lcost.codex_collector import (
    collect_codex_data,
    detect_codex_surface,
    find_codex_session_files,
    parse_codex_session,
)
from lcost.ingest_state import new_ingest_state
from lcost.pricing import calculate_cost


ROLLOUT_ONE = "11111111-1111-4111-8111-111111111111"
ROLLOUT_TWO = "22222222-2222-4222-8222-222222222222"
TURN_ID = "33333333-3333-4333-8333-333333333333"


def _record(record_type, payload, timestamp="2026-08-25T14:30:00+00:00"):
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _rollout_records(rollout_id=ROLLOUT_ONE, turn_id=TURN_ID):
    total_usage = {
        "input_tokens": 1000,
        "cached_input_tokens": 300,
        "cache_write_input_tokens": 100,
        "output_tokens": 80,
        "reasoning_output_tokens": 20,
        "total_tokens": 1180,
    }
    return [
        _record("session_meta", {
            "id": rollout_id,
            "cwd": "/work/lcost",
            "originator": "codex_vscode",
            "model_provider": "amazon-bedrock",
        }),
        _record("event_msg", {"type": "task_started", "turn_id": turn_id}),
        _record("turn_context", {
            "turn_id": turn_id,
            "model": "openai.gpt-5.6-luna",
            "cwd": "/work/lcost",
        }),
        _record("event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": total_usage,
                "total_token_usage": total_usage,
            },
        }),
        # Codex can emit a repeated cumulative token event while completing a
        # task. It must not become a second query.
        _record("event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": total_usage,
                "total_token_usage": total_usage,
            },
        }),
        _record("event_msg", {"type": "task_complete", "turn_id": turn_id}),
    ]


def _write_rollout(root: Path, rollout_id=ROLLOUT_ONE, *,
                   archived=False, records=None) -> Path:
    directory = root / ("archived_sessions" if archived else "sessions") / "2026" / "08" / "25"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-25T14-30-00-{rollout_id}.jsonl"
    data = records if records is not None else _rollout_records(rollout_id)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in data),
        encoding="utf-8",
    )
    return path


class TestCodexCollector:
    def test_vscode_rollout_preserves_tokens_and_estimates_price(self, tmp_path):
        session_file = _write_rollout(tmp_path)

        entries, offset = parse_codex_session(session_file)

        assert offset == session_file.stat().st_size
        assert list(entries) == [f"codex:{TURN_ID}:1"]
        entry = entries[f"codex:{TURN_ID}:1"]
        assert entry["source"] == "codex"
        assert entry["surface"] == "vscode"
        assert entry["modelProvider"] == "amazon-bedrock"
        assert entry["project"] == "/work/lcost"
        assert entry["tokensIn"] == 600
        assert entry["tokensOut"] == 80
        assert entry["cacheWrites"] == 100
        assert entry["cacheReads"] == 300
        assert entry["reasoningTokens"] == 20
        assert entry["reasoningTokensAdditive"] is False
        assert entry["cost"] == pytest.approx(calculate_cost(
            600, 80, 100, 300, "openai.gpt-5.6-luna",
        ))

    def test_incremental_state_skips_unchanged_rollout(self, tmp_path):
        _write_rollout(tmp_path)
        state = new_ingest_state()

        first = collect_codex_data(False, ingest_state=state, codex_home=tmp_path)
        second = collect_codex_data(False, ingest_state=state, codex_home=tmp_path)

        assert len(first) == 1
        assert second == {}

    def test_turn_context_before_task_start_is_retained(self, tmp_path):
        records = _rollout_records()
        records[1], records[2] = records[2], records[1]
        session_file = _write_rollout(tmp_path, records=records)

        entries, _ = parse_codex_session(session_file)

        entry = entries[f"codex:{TURN_ID}:1"]
        assert entry["model"] == "openai.gpt-5.6-luna"
        assert entry["cost"] is not None

    def test_parent_replay_is_deduplicated_by_turn(self, tmp_path):
        _write_rollout(tmp_path, ROLLOUT_ONE)
        _write_rollout(tmp_path, ROLLOUT_TWO)

        entries = collect_codex_data(False, codex_home=tmp_path)

        assert list(entries) == [f"codex:{TURN_ID}:1"]

    def test_archived_copy_wins_over_duplicate_live_copy(self, tmp_path):
        live = _write_rollout(tmp_path)
        archived = _write_rollout(
            tmp_path, archived=True,
            records=_rollout_records() + [
                _record("event_msg", {"type": "task_complete", "turn_id": TURN_ID}),
            ],
        )

        files = find_codex_session_files(tmp_path)

        assert files == [archived]
        assert archived.stat().st_size > live.stat().st_size


@pytest.mark.parametrize(
    ("originator", "surface"),
    [
        ("codex_vscode", "vscode"),
        ("codex_cli", "codex-cli"),
        ("chatgpt_desktop", "chatgpt"),
        ("unknown", "codex"),
    ],
)
def test_detect_codex_surface(originator, surface):
    assert detect_codex_surface(originator) == surface
