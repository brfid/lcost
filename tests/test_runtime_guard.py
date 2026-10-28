"""The local hook and CI guard must both catch nested runtime files."""
from pathlib import Path
import subprocess

import pytest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "pre-commit"


@pytest.mark.parametrize("filename", [
    "ledger.json",
    "nested/ingest_state.json",
    "backups/ledger-2026-09-12.json",
    "backups/recalc-2026-10-28T120000000000.json",
])
def test_runtime_files_are_rejected_in_staged_and_tracked_checks(tmp_path, filename):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    for args in ([], ["--all"]):
        result = subprocess.run(["sh", str(GUARD), *args], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 1 and filename in result.stdout


def test_recalculation_backups_are_ignored_by_git(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ignore = GUARD.parent.parent / ".gitignore"
    (tmp_path / ".gitignore").write_text(ignore.read_text())
    filename = "nested/backups/recalc-2026-10-28T120000000000.json"
    result = subprocess.run(
        ["git", "check-ignore", filename],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0 and filename in result.stdout
