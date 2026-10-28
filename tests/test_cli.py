"""Exercise command-line contracts through the public entry point."""
from contextlib import nullcontext
from datetime import datetime, timedelta
import os
from unittest.mock import Mock

import pytest

from lcost.cli import main, parse_arguments
from lcost.ledger import save_ledger


def test_dashboard_receives_filters_and_scan_options(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    app = Mock()
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.setattr("lcost.tui.CostTrackerApp", app)
    monkeypatch.setattr("lcost.pidfile.single_instance", lambda **kwargs: nullcontext())
    monkeypatch.setattr("sys.argv", [
        "lcost", "--ledger-path", str(path), "--source", "cc", "--cached",
        "--rescan", "--deep", "--max-gap-hours", "12", "--project", "example",
        "--from", "2026-09-01", "--to", "2026-09-12", "--days", "7",
    ])
    assert main() == 0
    app.assert_called_once_with(
        ledger_path_override=str(path), source_filter="claude-code", no_ingest=True,
        force_ingest=True, project_filter="example", date_from="2026-09-01",
        date_to="2026-09-12", days=7, deep=True, max_gap_hours=12.0, demo=False,
    )
    app.return_value.run.assert_called_once_with()


@pytest.mark.parametrize("args", [
    ["--from", "2026-02-30"], ["--from", "2026-9-1"],
    ["--from", "2026-09-12", "--to", "2026-09-01"],
    ["--days", "0"], ["--days", "-1"],
    ["--max-gap-hours", "nan"], ["--max-gap-hours", "inf"], ["--max-gap-hours", "-1"],
    ["--color", "truecolor"],
])
def test_invalid_options_fail_before_loading_data(args, monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcost", *args])
    with pytest.raises(SystemExit) as exc:
        parse_arguments()
    assert exc.value.code == 2


def test_explicit_report_dates_override_default_day_limit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.json"
    start = datetime(2026, 8, 1)
    ledger = {str(i): {"ts": (start + timedelta(days=i)).isoformat(), "source": "cline", "cost": 1}
              for i in range(41)}
    save_ledger(path, ledger)
    monkeypatch.setattr("sys.argv", ["lcost", "--report", "--cached", "--ledger-path", str(path),
                                    "--from", "2026-08-01", "--to", "2026-09-12"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "All 41 Active Days" in output
    assert "2026-08-01" in output and "$41" in output


def test_report_active_days_can_be_far_apart(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.json"
    save_ledger(path, {"old": {"ts": "2020-01-01T12:00:00", "source": "cline", "cost": 1}})
    monkeypatch.setattr("sys.argv", ["lcost", "--report", "--cached", "--days", "7", "--ledger-path", str(path)])
    assert main() == 0
    assert "2020-01-01" in capsys.readouterr().out


def test_recalc_cli_honors_selection_and_previews_assumptions(tmp_path, monkeypatch, capsys):
    from tests.test_bedrock_pricing import usage
    from lcost.ledger import load_ledger

    path = tmp_path / "ledger.json"
    ledger = {"missing": usage(), "other": usage("openai.gpt-6-astra")}
    save_ledger(path, ledger)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    args = [
        "lcost", "--recalc", "--ledger-path", str(path),
        "--source", "codex", "--provider", "amazon-bedrock",
        "--model", "openai.gpt-6.1-sol", "--only-unpriced",
        "--assume-bedrock-route", "regional",
    ]
    monkeypatch.setattr("sys.argv", [*args, "--dry-run"])
    assert main() == 0
    assert load_ledger(path) == ledger
    preview = capsys.readouterr().out
    assert "Entries changed: 1" in preview and "assumed regional" in preview
    assert "gpt-6-astra" not in preview
    monkeypatch.setattr("sys.argv", args)
    assert main() == 0
    updated = load_ledger(path)
    assert updated["missing"]["cost"] == pytest.approx(0.044)
    assert updated["other"] == ledger["other"]
    assert "Backup:" in capsys.readouterr().out


def test_recalc_writer_refuses_an_active_dashboard(tmp_path, monkeypatch, capsys):
    from lcost.pidfile import single_instance

    path = tmp_path / "ledger.json"
    save_ledger(path, {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("sys.argv", ["lcost", "--recalc", "--ledger-path", str(path)])
    with single_instance():
        assert main() == 1
    assert "Stop the dashboard" in capsys.readouterr().out


@pytest.mark.parametrize("args", [
    ["--model", "gpt-6-sol"], ["--provider", "amazon-bedrock"],
    ["--only-unpriced"], ["--include-source-reported"],
    ["--assume-bedrock-route", "global"],
])
def test_recalc_selection_is_not_silently_ignored_by_other_modes(args, monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcost", "--report", *args])
    with pytest.raises(SystemExit):
        parse_arguments()


@pytest.mark.parametrize("original", [None, "", "1"])
@pytest.mark.parametrize("mode", ["auto", "always", "never"])
def test_dashboard_color_mode_and_environment_restoration(original, mode, monkeypatch):
    from rich.segment import Segment
    from rich.style import Style
    from textual.color import Color
    from lcost.tui import CostTrackerApp

    if original is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", original)
    before = dict(os.environ)
    observed = []
    palette = []

    def inspect_dashboard(app):
        observed.append(app.no_color)
        segments = [Segment("palette", Style(color="#ff9900", bgcolor="#cc6699"))]
        for line_filter in app.get_line_filters():
            segments = line_filter.apply(segments, Color.parse("#000000"))
        style = segments[0].style
        palette.append((style.color.triplet, style.bgcolor.triplet))
        # The option changes this dashboard, not its surrounding environment.
        assert dict(os.environ) == before

    monkeypatch.setattr(CostTrackerApp, "run", inspect_dashboard)
    monkeypatch.setattr("sys.argv", ["lcost", "--demo", "--color", mode])
    assert main() == 0
    expected_monochrome = mode == "never" or (mode == "auto" and original is not None)
    assert observed == [expected_monochrome]
    foreground, background = palette[0]
    if expected_monochrome:
        assert len(set(foreground)) == len(set(background)) == 1
    else:
        assert foreground == (255, 153, 0)
        assert background == (204, 102, 153)
    assert dict(os.environ) == before


@pytest.mark.parametrize("original,mode", [(None, "never"), ("", "always"), ("1", "always")])
def test_color_environment_restored_when_dashboard_creation_fails(original, mode, monkeypatch):
    from lcost.tui import CostTrackerApp

    if original is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", original)
    before = dict(os.environ)

    def failed_dashboard(self, **kwargs):
        raise RuntimeError("dashboard construction failed")

    monkeypatch.setattr(CostTrackerApp, "__init__", failed_dashboard)
    monkeypatch.setattr("sys.argv", ["lcost", "--demo", "--color", mode])
    with pytest.raises(RuntimeError, match="dashboard construction failed"):
        main()
    assert dict(os.environ) == before


@pytest.mark.parametrize("mode", ["auto", "always", "never"])
def test_color_option_keeps_reports_plain_and_environment_unchanged(mode, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "")
    before = dict(os.environ)
    monkeypatch.setattr("sys.argv", ["lcost", "--demo", "--report", "--days", "1",
                                    "--color", mode])
    assert main() == 0
    output = capsys.readouterr().out
    assert "DEMO DATA" in output and "TOTAL" in output
    assert "\x1b" not in output
    assert dict(os.environ) == before
