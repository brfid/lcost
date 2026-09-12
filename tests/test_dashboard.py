"""Keyboard-driven tests of the board, refreshes, filters and call inspection."""
import asyncio
from datetime import timedelta

import pytest
from textual.widgets import Label

from lcost.ledger import save_ledger
from lcost.ops_widgets import EntryDetailScreen, FluidBar, HeatmapGrid, LiveSparkline, LogRow
from lcost.tui import CostTrackerApp


def text(widget):
    return str(widget.render())


def labels(widget):
    return " ".join(text(label) for label in widget.query(Label))


def test_units_switch_the_whole_board_and_preserve_selection(
    tmp_path, dashboard_clock, dashboard_ledger,
):
    async def scenario():
        path = tmp_path / "ledger.json"
        save_ledger(path, dashboard_ledger)
        app = CostTrackerApp(ledger_path_override=str(path), no_ingest=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            strip = text(app.query_one("#hud-source-strip"))
            assert all(source in strip for source in ("ALL SOURCES", "Codex", "Claude", "Cline"))
            assert "$10" in labels(app.query_one("#hud-kpi-strip"))
            assert "PRICE" in str(app.query_one("#hud-trend").border_title)
            assert "expensive-project" in text(app.query_one("#hud-ranking Label"))
            assert "$1" in text(app.query(LogRow)[0].query_one(".log-col-cost"))
            await pilot.press("j", "j", "m")
            await pilot.pause()
            kpis = labels(app.query_one("#hud-kpi-strip"))
            assert "10.1 KTok" in kpis
            assert "1.4 KTok/day" in kpis
            assert "$" not in kpis
            assert "TOKENS" in text(app.query_one("#hud-source-strip"))
            assert "TOKENS" in str(app.query_one("#hud-trend").border_title)
            assert "TOKENS" in str(app.query_one("#hud-heatmap").border_title)
            assert "TOKENS" in str(app.query_one("#hud-hourly").border_title)
            assert "cached-project" in text(app.query_one("#hud-ranking Label"))
            project_bar = app.query_one("#hud-ranking FluidBar", FluidBar)
            assert "█" in text(project_bar)
            assert len(text(project_bar)) == project_bar.size.width
            first = app.query(LogRow)[0]
            assert "10 KTok" in text(first.query_one(".log-col-cost"))
            assert "▓▓▓▓▓▓▓▓" in text(first.query_one(".log-col-bar"))
            assert "m units: tokens" in text(app.query_one("#bottom-status"))
            detail = text(app.query_one("#query-detail-content"))
            assert "expensive-project" in detail
            assert "QUERY PRICE" in detail and "TOTAL TOKENS" in detail

            # Hourly requests remain available; m brings it back into board units.
            await pilot.press("h")
            assert app._hourly_metric == "requests"
            await pilot.press("m")
            await pilot.pause()
            assert "$10" in labels(app.query_one("#hud-kpi-strip"))
            assert "COST" in str(app.query_one("#hud-hourly").border_title)
            assert "expensive-project" in text(app.query_one("#query-detail-content"))

            # Expanded panels retain their detailed metric cycles.
            await pilot.press("1", "m", "escape", "2", "m", "escape")
            await pilot.pause()
            assert app._trend_metric == "tokens_io"
            assert app._heatmap_metric == "requests"
            assert "$10" in labels(app.query_one("#hud-kpi-strip"))
            await pilot.press("m", "m", "m", "m")
            await pilot.pause()
            assert "$10" in labels(app.query_one("#hud-kpi-strip"))

    asyncio.run(scenario())


def test_refresh_updates_old_records_rolls_over_day_and_keeps_selected_call(
    tmp_path, dashboard_clock, dashboard_ledger,
):
    async def scenario():
        path = tmp_path / "ledger.json"
        old = dashboard_clock.now - timedelta(days=2)
        dashboard_ledger["old"] = dict(dashboard_ledger["cline:expensive"], ts=old.isoformat(), cost=2)
        dashboard_ledger["spawn"] = {
            "source": "agent_spawn",
            "ts": (dashboard_clock.now + timedelta(minutes=5)).astimezone().isoformat(),
        }
        save_ledger(path, dashboard_ledger)
        app = CostTrackerApp(ledger_path_override=str(path), no_ingest=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("j", "j")
            assert sum(map(sum, app._get_hm_grid("cost"))) == 12
            dashboard_ledger["old"]["cost"] = 20
            save_ledger(path, dashboard_ledger)
            app._auto_refresh_tick()
            await pilot.pause()
            assert sum(map(sum, app._get_hm_grid("cost"))) == 30
            assert "$30" in str(app.query_one("#hud-trend").border_subtitle)

            dashboard_ledger["new"] = dict(dashboard_ledger["codex:cached"],
                ts=(dashboard_clock.now + timedelta(seconds=1)).isoformat(), project="new-project")
            save_ledger(path, dashboard_ledger)
            app._auto_refresh_tick()
            await pilot.pause()
            assert "expensive-project" in text(app.query_one("#query-detail-content"))
            await pilot.press("enter")
            assert isinstance(app.screen, EntryDetailScreen)
            assert app.screen._entry_id == "cline:expensive"
            await pilot.press("escape")

            dashboard_clock.now += timedelta(days=1)
            app._auto_refresh_tick()
            await pilot.pause()
            assert not app.query("#hud-ranking")
            assert app._snapshot.overview.cost.today == 0
            assert "2026-09-13" in text(app.query_one("#top-elbow"))
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 50), (80, 24)])
def test_compact_board_switches_units_and_hides_inspector(
    tmp_path, dashboard_clock, dashboard_ledger, size,
):
    async def scenario():
        path = tmp_path / "ledger.json"
        save_ledger(path, dashboard_ledger)
        app = CostTrackerApp(ledger_path_override=str(path), no_ingest=True)
        async with app.run_test(size=size) as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert app.query_one("#hud-log-table").display
            assert not app.query_one("#hud-log-detail").display
            assert "10.1 KTok" in labels(app.query_one("#hud-kpi-strip"))
            await pilot.press("3", "j", "enter")
            assert isinstance(app.screen, EntryDetailScreen)
    asyncio.run(scenario())


def test_filters_apply_to_every_view_and_empty_ledger_is_usable(
    tmp_path, dashboard_clock, dashboard_ledger,
):
    async def scenario():
        path = tmp_path / "ledger.json"
        dashboard_ledger["codex:cached"]["promptPreview"] = "Literal [bold]text[/] and [not-a-tag]"
        save_ledger(path, dashboard_ledger)
        app = CostTrackerApp(ledger_path_override=str(path), no_ingest=True,
                             project_filter="CACHED", date_from="2026-09-12", date_to="2026-09-12")
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert list(app._ledger) == ["codex:cached"]
            heatmap = app.query_one(HeatmapGrid)
            assert len(heatmap._grid[0]) == 1
            assert heatmap._x_labels == [(0, "12")]
            assert len(app.query(LogRow)) == 1
            assert "$1" in text(app.query_one("#hud-source-strip"))
            assert sum(map(sum, app._get_hm_grid("cost"))) == 1
            await pilot.press("enter")
            assert "[not-a-tag]" in text(app.screen.query_one("#entry-detail-body"))
            await pilot.press("escape")
            await pilot.press("m")
            await pilot.pause()
            assert "10 KTok" in labels(app.query_one("#hud-kpi-strip"))
            sparks = list(app.query(LiveSparkline))
            assert any(any(spark.data) for spark in sparks)
            save_ledger(path, {})
            app._load_data()
            await pilot.pause()
            assert all(not any(spark.data) for spark in sparks)
            app._auto_refresh_tick()
            await pilot.pause()
            await pilot.press("m", "enter", "1", "escape")
            await pilot.pause()
            assert "No queries" in text(app.query_one("#query-detail-content"))
            assert len(app.query_one(HeatmapGrid)._grid[0]) == 24
            assert all(not any(spark.data) for spark in app.query(LiveSparkline))
    asyncio.run(scenario())


def test_heatmap_fits_usage_in_both_views_and_expands_on_refresh(
    tmp_path, dashboard_clock, dashboard_ledger,
):
    async def scenario():
        path = tmp_path / "ledger.json"
        base = dashboard_ledger["codex:cached"]
        for hour in (8, 17):
            dashboard_ledger[f"unpriced-{hour}"] = dict(
                base, ts=dashboard_clock.now.replace(hour=hour).isoformat(), cost=0,
            )
        save_ledger(path, dashboard_ledger)
        app = CostTrackerApp(ledger_path_override=str(path), no_ingest=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()

            def check_range(first, last):
                heatmap = app.query_one(HeatmapGrid)
                assert len(heatmap._grid) == 7
                assert len(heatmap._grid[0]) == last - first + 1
                ticks = [text(w) for w in heatmap.query(".heatmap-x-label")]
                assert ticks[0] == f"{first:02d}" and ticks[-1] == f"{last:02d}"
                return heatmap

            heatmap = check_range(8, 17)
            assert sum(map(sum, heatmap._grid)) == 10
            assert all(row[1] == 0 for row in heatmap._grid)  # 09:00 gap stays visible.
            await pilot.press("m")
            await pilot.pause()
            assert sum(map(sum, check_range(8, 17)._grid)) == 30_100
            await pilot.press("2", "m")
            await pilot.pause()
            assert sum(map(sum, check_range(8, 17)._grid)) == 4

            dashboard_ledger["late"] = dict(
                base, ts=dashboard_clock.now.replace(hour=23).isoformat(),
            )
            save_ledger(path, dashboard_ledger)
            app._auto_refresh_tick()
            await pilot.pause()
            check_range(8, 23)
            await pilot.press("escape")
            await pilot.pause()
            check_range(8, 23)

            dashboard_ledger["midnight"] = dict(
                base, ts=dashboard_clock.now.replace(hour=0).isoformat(),
            )
            save_ledger(path, dashboard_ledger)
            app._auto_refresh_tick()
            await pilot.pause()
            check_range(0, 23)
    asyncio.run(scenario())
