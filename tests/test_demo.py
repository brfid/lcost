"""The demo must work on a machine with no source data or writable ledger."""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from lcost.aggregation import entry_local_dt
from lcost.cli import main
from lcost.demo import build_demo_ledger
from lcost.ops_widgets import FluidBar
from lcost.tui import CostTrackerApp


def no_personal_data(*args, **kwargs):
    raise AssertionError("Demo must not access personal usage files")


def test_demo_is_repeatable_and_includes_all_sources(dashboard_clock):
    ledger = build_demo_ledger(dashboard_clock.now)
    assert ledger == build_demo_ledger(dashboard_clock.now)
    assert {entry["source"] for entry in ledger.values()} == {"cc", "cline", "codex"}
    assert all(entry["project"].startswith("demo/") for entry in ledger.values())


def test_demo_has_work_sessions_breaks_and_quiet_days():
    now = datetime(2026, 9, 11, 23, 59)
    ledger = build_demo_ledger(now)
    days = Counter()
    sessions = defaultdict(list)
    for entry in ledger.values():
        stamp = entry_local_dt(entry)
        assert 8 <= stamp.hour < 22
        assert stamp <= now
        days[stamp.date()] += 1
        sessions[entry["session"]].append(entry)

    assert len(days) < 50  # Days off, not an always-on calendar.
    weekdays = [now.date() - timedelta(days=i) for i in range(60)
                if (now - timedelta(days=i)).weekday() < 5]
    weekends = [now.date() - timedelta(days=i) for i in range(60)
                if (now - timedelta(days=i)).weekday() >= 5]
    assert sum(days[d] for d in weekends) / len(weekends) < (
        sum(days[d] for d in weekdays) / len(weekdays) / 3
    )
    gaps = []
    for calls in sessions.values():
        assert len({(e["source"], e["model"], e["project"], e["promptPreview"])
                    for e in calls}) == 1
        stamps = sorted(entry_local_dt(e) for e in calls)
        assert stamps[-1] - stamps[0] <= timedelta(minutes=65)
        gaps.extend((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
    assert sum(gap < 120 for gap in gaps) > len(gaps) / 2
    assert any(gap > 5 * 60 for gap in gaps)


def test_demo_refresh_preserves_calls_and_only_reveals_elapsed_work():
    morning = datetime(2026, 9, 11, 14)
    early = build_demo_ledger(morning)
    late = build_demo_ledger(morning.replace(hour=23))
    assert early.items() <= late.items()
    assert len(early) < len(late)
    assert any(entry_local_dt(entry).date() == morning.date() for entry in early.values())
    assert all(entry_local_dt(entry) <= morning for entry in early.values())
    assert late == build_demo_ledger(morning.replace(hour=23).astimezone(timezone.utc))

    next_day = morning.replace(hour=0) + timedelta(days=1)
    rolled = build_demo_ledger(next_day)
    retained = {key: entry for key, entry in late.items()
                if entry_local_dt(entry) >= next_day - timedelta(days=59)}
    assert rolled == retained
    # Opening before work must not compress a day's calls into the night.
    assert rolled == build_demo_ledger(next_day.replace(hour=7))


def test_demo_dashboard_runs_and_switches_units_without_source_data(monkeypatch, dashboard_clock):
    for name in ("get_ledger_path", "load_ledger", "run_ingest", "load_ingest_state"):
        monkeypatch.setattr(f"lcost.tui.{name}", no_personal_data)

    async def scenario():
        app = CostTrackerApp(demo=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert "DEMO DATA" in str(app.query_one("#top-title").render())
            assert app._snapshot.overview.cost.today > 0
            await pilot.press("m")
            await pilot.pause()
            assert not app.query("#hud-source-strip")
            assert "TOKENS" in str(app.query_one("#hud-trend").border_title)
            assert app._snapshot.overview.tokens.today > 0
            assert app._ops_row_specs
            await pilot.press("t", "1")
            await pilot.pause()
            assert app._trend_view == "models" and app._current_tab == "TREND"
            await pilot.press("escape")
            await pilot.resize_terminal(170, 52)
            await pilot.pause()
            for bar in app.query(FluidBar):
                assert "█" in str(bar.render())
                assert len(str(bar.render())) == bar.size.width
    asyncio.run(scenario())


def test_demo_report_does_not_read_or_write_usage(monkeypatch, capsys):
    for name in ("get_ledger_path", "load_ledger", "run_ingest"):
        monkeypatch.setattr(f"lcost.cli.{name}", no_personal_data)
    monkeypatch.setattr("sys.argv", ["lcost", "--demo", "--report", "--days", "7"])
    assert main() == 0
    assert "DEMO DATA" in capsys.readouterr().out
