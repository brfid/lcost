"""Tests for pipeline gap detection, backup rotation, and orphan cleanup."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from lcost.ledger import (
    backup_dir,
    hours_since_last_ingest,
    load_ingest_state,
    prune_orphan_file_state,
    rotate_backup,
    save_ingest_state,
    save_ledger,
    stamp_ingest,
)
from lcost.ingest_state import source_health
from lcost.cli import print_ledger_stats
from lcost.pipeline import run_ingest




class TestIngestTimestamp:
    def test_stamp_and_read(self, tmp_path):
        state = {"_version": 1, "files": {}, "last_ingest_at": None}
        stamp_ingest(state)
        gap = hours_since_last_ingest(state)
        assert gap is not None
        assert 0 <= gap < 0.01  # under 36 seconds

    def test_unknown_timestamp(self):
        assert hours_since_last_ingest({"last_ingest_at": None}) is None
        assert hours_since_last_ingest({}) is None
        assert hours_since_last_ingest({"last_ingest_at": "not-a-date"}) is None

    def test_stale_timestamp(self):
        two_days_ago = (datetime.now() - timedelta(hours=48)).isoformat()
        gap = hours_since_last_ingest({"last_ingest_at": two_days_ago})
        assert 47.9 < gap < 48.1


class TestBackupRotation:
    def test_creates_backup(self, tmp_path):
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"entry1": {"cost": 1.0}})
        result = rotate_backup(ledger_path)
        today = datetime.now().strftime("%Y-%m-%d")
        assert result == backup_dir(ledger_path) / f"ledger-{today}.json"
        assert result.exists()
        assert json.loads(result.read_text()) == {"entry1": {"cost": 1.0}}

    def test_idempotent_per_day(self, tmp_path):
        """Running backup twice same day is a no-op."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        first = rotate_backup(ledger_path)
        second = rotate_backup(ledger_path)
        assert first is not None
        assert second is None  # idempotent

    def test_missing_source_skipped(self, tmp_path):
        """No ledger file → no backup."""
        ledger_path = tmp_path / "missing.json"
        assert rotate_backup(ledger_path) is None

    def test_retention_prunes_old(self, tmp_path):
        """Only keep N most recent backups."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {})
        d = backup_dir(ledger_path)
        d.mkdir(parents=True, exist_ok=True)
        # Create 10 fake old backups
        for i in range(10):
            (d / f"ledger-2020-01-{i:02d}.json").write_text("{}")
        rotate_backup(ledger_path, retain=3)
        remaining = sorted(d.glob("ledger-*.json"))
        # Should be 3 total: today's + 2 oldest-keepers (sorted lex)
        assert len(remaining) == 3
        # The newest is today's
        today = datetime.now().strftime("%Y-%m-%d")
        assert remaining[-1].name == f"ledger-{today}.json"


class TestOrphanCleanup:
    def test_drops_missing_files(self, tmp_path):
        real = tmp_path / "real.jsonl"
        real.write_text("")
        state = {
            "_version": 1,
            "files": {
                str(real): {"size": 0, "byte_offset": 0},
                "/does/not/exist.jsonl": {"size": 100, "byte_offset": 50},
                "/also/gone.jsonl": {"size": 0, "byte_offset": 0},
            },
        }
        pruned = prune_orphan_file_state(state)
        assert pruned == 2
        assert str(real) in state["files"]
        assert "/does/not/exist.jsonl" not in state["files"]


class TestGapDetection:
    def test_first_run_no_gap_trigger(self, tmp_path, capsys):
        """First run: no previous timestamp, so no gap-triggered deep mode."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {})
        ledger = {}

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False)

        captured = capsys.readouterr()
        assert "Deep rescan" not in captured.out

    def test_fresh_ingest_no_trigger(self, tmp_path, capsys):
        """Previous run <24h ago: stays incremental."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        # Seed an ingest-state that says we ran 1 hour ago
        state_path = ledger_path.parent / "ingest_state.json"
        recent = (datetime.now() - timedelta(hours=1)).isoformat()
        save_ingest_state(state_path, {
            "_version": 1, "files": {}, "last_ingest_at": recent,
        })

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False)

        captured = capsys.readouterr()
        assert "Deep rescan" not in captured.out

    def test_stale_ingest_triggers_deep(self, tmp_path, capsys):
        """Previous run >24h ago: auto-promote to deep mode."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        stale = (datetime.now() - timedelta(hours=48)).isoformat()
        save_ingest_state(state_path, {
            "_version": 1,
            "files": {"/some/old/file.jsonl": {"size": 100, "byte_offset": 50}},
            "last_ingest_at": stale,
        })

        with patch("lcost.pipeline.collect_cline_data", return_value={}) as cline, \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}) as cc, \
             patch("lcost.pipeline.collect_codex_data", return_value={}) as codex:
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       max_gap_hours=24.0)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out
        # Collectors called with fresh ingest_state (no old file entry)
        cline_state = cline.call_args.kwargs["ingest_state"]
        cc_state = cc.call_args.kwargs["ingest_state"]
        codex_state = codex.call_args.kwargs["ingest_state"]
        assert "/some/old/file.jsonl" not in cline_state.get("files", {})
        assert "/some/old/file.jsonl" not in cc_state.get("files", {})
        assert "/some/old/file.jsonl" not in codex_state.get("files", {})

    def test_custom_gap_threshold(self, tmp_path, capsys):
        """User can tune the threshold."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        save_ingest_state(state_path, {
            "_version": 1, "files": {},
            "last_ingest_at": (datetime.now() - timedelta(hours=2)).isoformat(),
        })

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            # 1-hour threshold: 2h gap should trigger
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       max_gap_hours=1.0)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out

    def test_explicit_deep_flag(self, tmp_path, capsys):
        """--deep forces rescan even with fresh ingest state."""
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        save_ingest_state(state_path, {
            "_version": 1, "files": {},
            "last_ingest_at": datetime.now().isoformat(),
        })

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       deep=True)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out


class TestBackupIntegration:
    def test_run_ingest_creates_backup(self, tmp_path):
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {"pre": {"cost": 1.0}})
        ledger = {"pre": {"cost": 1.0}}

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=True)

        today = datetime.now().strftime("%Y-%m-%d")
        assert (backup_dir(ledger_path) / f"ledger-{today}.json").exists()


class TestTimestampPersisted:
    def test_ingest_stamps_state(self, tmp_path):
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {})
        ledger = {}

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=True)

        state = load_ingest_state(ledger_path.parent / "ingest_state.json")
        assert state["last_ingest_at"] is not None


class TestSourceHealth:
    def test_records_each_successful_source_scan(self, tmp_path):
        ledger_path = tmp_path / "ledger.json"
        ledger = {}
        codex_entry = {
            "codex:one": {
                "source": "codex",
                "ts": "2026-08-25T10:00:00+00:00",
                "tokensIn": 10,
                "tokensOut": 2,
                "cost": 0.01,
            },
        }

        with patch("lcost.pipeline.collect_cline_data", return_value={}), \
             patch("lcost.pipeline.collect_claude_code_data", return_value={}), \
             patch("lcost.pipeline.collect_codex_data", return_value=codex_entry):
            assert run_ingest(ledger_path, ledger, quiet=True) == 1

        state = load_ingest_state(ledger_path.parent / "ingest_state.json")
        health = source_health(state)
        assert health["cline"]["records_seen"] == 0
        assert health["cc"]["records_seen"] == 0
        assert health["codex"]["records_seen"] == 1
        assert health["codex"]["records_added"] == 1
        assert health["codex"]["last_error"] is None

    def test_one_collector_error_does_not_hide_other_sources(self, tmp_path):
        ledger_path = tmp_path / "ledger.json"
        ledger = {}

        with patch(
            "lcost.pipeline.collect_cline_data",
            side_effect=OSError("Cline is being rewritten"),
        ), patch(
            "lcost.pipeline.collect_claude_code_data", return_value={},
        ), patch(
            "lcost.pipeline.collect_codex_data", return_value={},
        ):
            assert run_ingest(ledger_path, ledger, quiet=True) == 0

        state = load_ingest_state(ledger_path.parent / "ingest_state.json")
        health = source_health(state)
        assert health["cline"]["last_error"].startswith("OSError:")
        assert health["cc"]["last_error"] is None
        assert health["codex"]["last_error"] is None

    def test_stats_exposes_source_freshness(self, tmp_path, capsys):
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {})
        save_ingest_state(ledger_path.parent / "ingest_state.json", {
            "_version": 2,
            "files": {},
            "sources": {
                "codex": {
                    "last_attempt_at": datetime.now().isoformat(),
                    "last_success_at": datetime.now().isoformat(),
                    "last_error": None,
                    "records_seen": 12,
                    "records_added": 3,
                },
            },
            "last_ingest_at": datetime.now().isoformat(),
        })

        print_ledger_stats(ledger_path, {})

        output = capsys.readouterr().out
        assert "Source health:" in output
        assert "Codex / ChatGPT" in output
        assert "12 seen" in output
        assert "Claude Code" in output


@pytest.mark.parametrize("initial, correction, key, expected", [
    ({"source": "cline", "cost": 0, "tokensIn": 0},
     {"source": "cline", "cost": 0.12, "tokensIn": 1000}, "cost", 0.12),
    ({"source": "cc", "cost": 1},
     {"source": "cc", "cost": 1, "project": "recovered"}, "project", "recovered"),
    ({"source": "cc", "cost": None},
     {"source": "cc", "cost": 0.12}, "cost", 0.12),
])
def test_corrections_persist_even_without_new_entries(tmp_path, initial, correction, key, expected):
    from lcost.ledger import load_ledger
    path = tmp_path / "ledger.json"
    ledger = {"same-id": initial}
    save_ledger(path, ledger)
    source = "cline" if initial["source"] == "cline" else "claude-code"
    collector = "collect_cline_data" if source == "cline" else "collect_claude_code_data"
    with patch(f"lcost.pipeline.{collector}", return_value={"same-id": correction}):
        assert run_ingest(path, ledger, source=source, quiet=True) == 0
    assert load_ledger(path)["same-id"][key] == expected


def test_same_size_source_rewrite_is_reparsed(tmp_path):
    from lcost.ingest_state import file_needs_processing, update_file_state
    import os
    path = tmp_path / "source.jsonl"
    path.write_text("before")
    state = {"files": {}}
    update_file_state(state, path, path.stat().st_size)
    assert file_needs_processing(path, state) == (False, 0)
    previous = path.stat().st_mtime
    path.write_text("after!")
    os.utime(path, (previous + 2, previous + 2))
    assert file_needs_processing(path, state) == (True, 0)
