"""LCARS-style TUI dashboard for lcost."""

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Label, Static

from textual_plotext import PlotextPlot

from .aggregation import aggregate_by_day, calendar_days, iter_entries
from .demo import build_demo_ledger
from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    SOURCE_DISPLAY,
    SOURCE_MAP,
    format_cost,
    format_number,
    format_tokens,
    total_tokens,
)
from .ingest_state import (
    get_ingest_state_path,
    load_ingest_state,
    source_health,
)
from .ledger import get_ledger_path, load_ledger
from .pipeline import run_ingest
from .ops_data import (
    LOG_ROW_CAP,
    OpsView,

    RowSpec,
    build_row_specs,
    cost_bar,
    hour_week_grid,
    hourly_by_model,
    iter_individual_entries,
    model_color,
    row_activity_text,
    short_model,
    short_project,
    short_tools,
)
from .live_metrics import ClockBucket, LiveMetrics, MetricsSnapshot, compute_snapshot
from .ops_widgets import (
    LOG_COLUMNS,
    EntryDetailScreen,
    FluidBar,
    HeatmapGrid,
    HelpScreen,
    LiveBorderSubtitle,
    LiveHourlyBar,
    LiveLabel,
    LiveSparkline,
    LiveStatic,
    LogRow,
)

# Single-board model. The board is the only home view; number keys expand
# one panel to fullscreen, Escape/0 returns to the board.
#
# Expandable fullscreen views and their number keys:
#   1 TREND    daily chart or last-24h model lines (t switches view)
#   2 HEATMAP  hour×weekday    (m cycles cost/requests/tokens)
#   3 LOG      call log        (j/k, enter detail)
#   4 CALLS    per-call cost histogram (board-only-absent; expand-only)
EXPANDABLE = ["TREND", "HEATMAP", "LOG", "CALLS"]

# The board home view sentinel.
BOARD = "BOARD"

# Metric cycle orders, shared between the keypress handler that advances the
# metric and the panel builder that labels the "next" step.
TREND_CYCLE = {
    "cost": "tokens_io",
    "tokens_io": "tokens_cache",
    "tokens_cache": "savings",
    "savings": "cost",
}
HEATMAP_CYCLE = {"cost": "requests", "requests": "tokens", "tokens": "cost"}
HOURLY_CYCLE = {"cost": "tokens", "tokens": "requests", "requests": "cost"}




# ── Heatmap day-axis labels ──


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── Heatmap color ramps ──
# Shared by the board and fullscreen heatmap.
# Each entry: (color_zero, color_low, color_high)
HM_COLORS = {
    # (color_zero, color_low, color_high)
    # color_low is intentionally close to color_zero: with the sqrt scale +
    # 1% floor in HeatmapGrid._cell_color, low-activity cells start near-blank
    # and only climb toward color_high as intensity grows.
    "cost":     ((30, 20, 0),   (40, 22, 0),   (255, 140, 0)),
    "requests": ((20, 15, 30),  (25, 18, 35),  (180, 100, 200)),
    "tokens":   ((15, 15, 30),  (20, 18, 35),  (153, 153, 204)),
}


# ── Main app ──

# Maximum rows per ranking panel
TOP_N_PANEL = 6

# Model trend colors follow descending usage, keeping its leading lines
# distinct even when several models belong to the same family.
MODEL_TREND_COLORS = (
    (255, 153, 0),    # amber
    (86, 200, 255),   # cyan
    (228, 125, 208),  # pink
    (155, 221, 111),  # lime
    (187, 170, 255),  # lavender
    (240, 228, 66),   # yellow
    (255, 255, 255),  # white
    (213, 94, 0),     # rust
)
MODEL_TREND_MIN_SHARE = 0.01

# Stop-reason colors
_STOP_COLORS = {
    "end_turn": "#9999CC",
    "tool_use": "#FF9900",
    "max_tokens": "#CC6699",
    "stop_sequence": "#CC9966",
    "—": "#555566",
}


class CostTrackerApp(App):
    CSS_PATH = Path(__file__).resolve().parent / "lcost.tcss"
    TITLE = "lcost"
    REFRESH_INTERVAL = 30
    NEW_ROW_HIGHLIGHT_TICKS = 2
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        # priority=True is required: without it the Screen's
        # ctrl+c → copy_text binding wins. This also overrides Textual's
        # default ctrl+c → help_quit nag so ctrl+c quits gracefully.
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("r", "toggle_refresh", "Toggle refresh"),
        Binding("?", "show_help", "Help"),
        # Return to the board from any fullscreen panel
        Binding("escape", "board", "Board", show=False),
        Binding("0", "board", "Board", show=False),
        # Cycle through fullscreen panels
        Binding("]", "expand_next", "Next panel", show=False),
        Binding("[", "expand_prev", "Prev panel", show=False),
        # Row navigation (board/LOG call log) / scroll (other panels)
        Binding("j", "scroll_log(1)", "Scroll ↓", show=False),
        Binding("k", "scroll_log(-1)", "Scroll ↑", show=False),
        Binding("J", "scroll_log(10)", "Page ↓", show=False),
        Binding("K", "scroll_log(-10)", "Page ↑", show=False),
        Binding("ctrl+d", "scroll_log(10)", "Page ↓", show=False),
        Binding("ctrl+u", "scroll_log(-10)", "Page ↑", show=False),
        Binding("g", "jump_top", "Top", show=False),
        Binding("G", "jump_bottom", "Bottom", show=False),
        Binding("enter", "expand_selected", "Expand row", show=False),
        # Number shortcuts — expand a panel to fullscreen. 0/esc → board.
        #   1 TREND   2 HEATMAP   3 LOG   4 CALLS
        Binding("1", "expand('TREND')", "Trend", show=False),
        Binding("2", "expand('HEATMAP')", "Heatmap", show=False),
        Binding("3", "expand('LOG')", "Call log", show=False),
        Binding("4", "expand('CALLS')", "Calls", show=False),
        # Unified metric toggle — board switches its price/tokens view;
        # a fullscreen panel cycles its own metric (TREND/HEATMAP).
        Binding("m", "toggle_metric", "Units / metric", show=False),
        Binding("t", "toggle_trend_view", "Daily / models 24h", show=False),
        # Hourly-bar metric toggle (cost / tokens / requests)
        Binding("h", "toggle_hourly_metric", "Toggle hourly metric",
                show=False),
    ]


    def __init__(self, ledger_path_override=None, source_filter="all",
                 no_ingest=False, force_ingest=False, project_filter=None,
                 date_from=None, date_to=None, days=None, deep=False,
                 max_gap_hours=24.0, demo=False, **kwargs):
        super().__init__(**kwargs)
        self._ledger_path_override = ledger_path_override
        self._source_filter_arg = source_filter
        self._no_ingest = no_ingest
        self._force_ingest = force_ingest
        self._project_filter = project_filter
        self._date_from = date_from
        self._date_to = date_to
        self._days = days
        self._deep = deep
        self._max_gap_hours = max_gap_hours
        self._demo = demo
        self._data_revision = 0
        self._ledger: Dict = {}
        self._daily: Dict = {}
        self._source_filter: Optional[str] = None
        # Active view: BOARD (home) or one of EXPANDABLE when fullscreen.
        self._current_tab: str = BOARD
        self._auto_refresh: bool = True
        self._refresh_timer = None
        # Diff tracking for auto-refresh highlight: id → ticks-remaining
        self._new_entry_ids: Dict[str, int] = {}
        self._seen_ids: set = set()
        # Selected row index in the call log; -1 means no selection
        self._selected_row: int = -1
        # Per-row spec cache so selection moves don't rebuild labels
        self._ops_row_specs: List[RowSpec] = []
        # Per-panel metric state — sticky across expand/collapse.
        # TREND: cost → tokens_io → tokens_cache → savings
        self._trend_metric: str = "cost"
        self._trend_view: str = "daily"

        # HEATMAP: cost → requests → tokens
        self._heatmap_metric: str = "cost"
        # Board hourly bar cycles: "cost" → "tokens" → "requests"
        self._hourly_metric: str = "cost"
        # All monetary board values switch between price and total tokens.
        self._board_metric: str = "cost"

        # Reactive snapshot carrier — mounted in compose(); every live
        # widget subscribes to its `snapshot` attribute.
        self._metrics = LiveMetrics()
        # Rebuild static panels on data edits or a calendar-day rollover.
        self._last_render_sig: Optional[tuple] = None
        self._hm_grids: Dict[str, list] = {}

    def _load_data(self) -> MetricsSnapshot:
        """Reload ledger, recompute aggregates, push a fresh snapshot.

        Compare complete filtered records so corrections, metadata edits and
        filter changes invalidate cached views even without a new entry.
        """
        clock = ClockBucket.capture()
        if self._demo:
            ledger = build_demo_ledger(clock.now)
            ingest_state = {}
        else:
            ledger_path = get_ledger_path(self._ledger_path_override)
            ledger = load_ledger(ledger_path)
            run_ingest(ledger_path, ledger, source=self._source_filter_arg,
                       no_ingest=self._no_ingest, force_ingest=self._force_ingest,
                       deep=self._deep, max_gap_hours=self._max_gap_hours, quiet=True)
            ingest_state = load_ingest_state(get_ingest_state_path(ledger_path))
        self._deep = self._force_ingest = False
        arg = self._source_filter_arg
        self._source_filter = None if arg == "all" else SOURCE_MAP.get(arg, arg)
        date_from = self._date_from
        date_to = self._date_to
        if not (self._date_from or self._date_to) and self._days is not None:
            date_from = calendar_days(clock.now, self._days)[0]
            date_to = clock.day
        view = {eid: entry for _, eid, entry in iter_entries(
            ledger, self._source_filter, date_from, date_to, self._project_filter,
        )}
        if view != self._ledger:
            self._data_revision += 1
            self._hm_grids.clear()
        self._ledger = view
        self._daily = aggregate_by_day(view)

        current_ids = set(self._ledger.keys())
        if self._seen_ids:
            # Age existing highlights
            self._new_entry_ids = {
                eid: ticks - 1
                for eid, ticks in self._new_entry_ids.items()
                if ticks > 1 and eid in current_ids
            }
            for eid in current_ids - self._seen_ids:
                self._new_entry_ids[eid] = self.NEW_ROW_HIGHLIGHT_TICKS
        self._seen_ids = current_ids

        snap = compute_snapshot(
            self._ledger, self._daily, self._source_filter, clock=clock,
            source_health=source_health(ingest_state),
        )
        self._metrics.update(snap)
        return snap

    @property
    def _snapshot(self) -> Optional[MetricsSnapshot]:
        return self._metrics.snapshot

    def compose(self) -> ComposeResult:
        # Non-visible reactive carrier. Every live widget watches its
        # `snapshot` attribute.
        yield self._metrics

        with Horizontal(id="top-bar"):
            # Stardate-flavored timestamp: month · ISO-week · day-of-month.
            # Bound to the snapshot so it rolls over with the clock.
            yield LiveStatic(
                self._metrics,
                lambda s: s.clock.now.strftime("%Y-%m-%d (W%V)"),
                placeholder=datetime.now().strftime("%Y-%m-%d (W%V)"),
                id="top-elbow",
            )
            yield Static("lcost · DEMO DATA" if self._demo else "lcost", id="top-title")
            yield Static("", id="top-bar-line")

        with Horizontal():
            with VerticalScroll(id="main-content"):
                yield Vertical(id="panel-container")


        with Horizontal(id="bottom-bar"):
            yield Static("", id="bottom-status")

            yield Static("", id="bottom-bar-line")

    # Terminal height below this collapses the board mid-tier (charts).
    # Lowered as inter-section row gaps were removed, so the charts fit
    # in shorter terminals before the mid tier has to collapse.
    HUD_COMPACT_ROWS = 34


    def on_mount(self) -> None:
        snap = self._load_data()
        self._last_render_sig = (self._data_revision, snap.clock.day)
        self._update_status_bar()
        self._render_board()
        self._refresh_timer = self.set_interval(
            self.REFRESH_INTERVAL, self._auto_refresh_tick
        )

    def on_resize(self, event) -> None:
        if self._current_tab == BOARD:
            # App.size still holds the previous dimensions during Resize.
            self.call_after_refresh(self._apply_hud_density)

    def _apply_hud_density(self) -> None:
        """Adapt board density to the available terminal dimensions."""
        try:
            container = self.query_one("#panel-container", Vertical)
        except Exception:
            return
        if self.size.height < self.HUD_COMPACT_ROWS:
            container.add_class("hud-compact")
        else:
            container.remove_class("hud-compact")
        if self.size.width < 130:
            container.add_class("hud-detail-collapsed")
        else:
            container.remove_class("hud-detail-collapsed")


    def _update_status_bar(self) -> None:
        entry_count = len(self._ledger)
        day_count = len(self._daily)
        refresh_icon = "⟳" if self._auto_refresh else "⏸"
        new_count = len(self._new_entry_ids)
        new_badge = f" · [#FF9900]+{new_count} new[/]" if new_count else ""
        dot = " [#9999CC]◤[/] "
        unit = "tokens" if self._board_metric == "tokens" else "price"
        uses_board_units = self._current_tab == BOARD or (
            self._current_tab == "TREND" and self._trend_view == "models"
        )
        metric_hint = f"m units: {unit}" if uses_board_units else "m metric"
        trend_hint = " · t daily/models" if self._current_tab in {BOARD, "TREND"} else ""
        hint = (f"{dot}1-4 expand{dot}esc board{dot}{metric_hint}{trend_hint} · h hourly"
                f"{dot}j/k · ENTER details{dot}r pause{dot}? help{dot}q quit")

        filters = [
            SOURCE_DISPLAY.get(self._source_filter, self._source_filter),
            self._project_filter, self._date_from, self._date_to,
        ]
        filter_text = " · ".join(self._esc_markup(str(v)) for v in filters if v)
        if self._days and not (self._date_from or self._date_to):
            filter_text += f" · {self._days} calendar days"
        if filter_text:
            hint = f"{dot}{filter_text}" + hint
        status = self.query_one("#bottom-status", Static)
        unpriced = sum(day.get("unpricedRequests", 0) for day in self._daily.values())
        unpriced_tokens = sum(day.get("unpricedTokens", 0) for day in self._daily.values())
        partial = (
            f"[#FF9900]PARTIAL PRICE · {unpriced:,} unpriced · "
            f"{format_tokens(unpriced_tokens)}[/]{dot}" if unpriced else ""
        )
        status.update(
            f"  [#FF9900]{entry_count:,}[/] entries{dot}"
            f"[#FF9900]{day_count}[/] days{dot}"
            f"{partial}{refresh_icon} {self.REFRESH_INTERVAL}s{new_badge}{hint}  ",
        )

    # Views with a selectable/scrollable call log (board + fullscreen LOG).
    _LOG_VIEWS = frozenset({BOARD, "LOG"})

    def _auto_refresh_tick(self) -> None:
        if not self._auto_refresh:
            return
        self._force_ingest = False
        snap = self._load_data()
        self._update_status_bar()

        data_changed = (self._data_revision, snap.clock.day) != self._last_render_sig
        self._last_render_sig = (self._data_revision, snap.clock.day)

        # Board: rebuild when data changes (chart + heatmap are static
        # widgets, not reactive; KPI cells self-update via LiveLabel
        # watchers). The call log rows refresh in place below.
        if self._current_tab == BOARD:
            if data_changed or self._trend_view == "models":
                self._render_board()
            else:
                self._refresh_log_rows()
            return

        # Fullscreen LOG refreshes rows in place; other fullscreen panels
        # are static charts — rebuild when records or the calendar day change.
        if self._current_tab == "LOG":
            self._refresh_log_rows()
        elif data_changed or (
            self._current_tab == "TREND" and self._trend_view == "models"
        ):
            self._render_panel(self._current_tab)

    def action_toggle_refresh(self) -> None:
        self._auto_refresh = not self._auto_refresh
        self._update_status_bar()

    def action_expand_next(self) -> None:
        if self._current_tab == BOARD:
            self.action_expand(EXPANDABLE[0])
            return
        idx = (EXPANDABLE.index(self._current_tab) + 1) % len(EXPANDABLE)
        self.action_expand(EXPANDABLE[idx])

    def action_expand_prev(self) -> None:
        if self._current_tab == BOARD:
            self.action_expand(EXPANDABLE[-1])
            return
        idx = (EXPANDABLE.index(self._current_tab) - 1) % len(EXPANDABLE)
        self.action_expand(EXPANDABLE[idx])

    def action_jump_top(self) -> None:
        if self._current_tab in self._LOG_VIEWS and self._ops_row_specs:
            self._set_selected_row(0)
        else:
            with contextlib.suppress(Exception):
                self.query_one("#main-content", VerticalScroll).scroll_home(animate=False)

    def action_jump_bottom(self) -> None:
        if self._current_tab in self._LOG_VIEWS and self._ops_row_specs:
            self._set_selected_row(len(self._ops_row_specs) - 1)
        else:
            with contextlib.suppress(Exception):
                self.query_one("#main-content", VerticalScroll).scroll_end(animate=False)

    def action_show_help(self) -> None:
        view = "TREND_MODELS" if (
            self._current_tab == "TREND" and self._trend_view == "models"
        ) else self._current_tab
        self.push_screen(HelpScreen(view))

    def action_scroll_log(self, delta: int) -> None:
        """In log views: move selection. Elsewhere: scroll container."""
        if self._current_tab in self._LOG_VIEWS:

            if not self._ops_row_specs:
                return
            max_idx = len(self._ops_row_specs) - 1
            # If nothing selected, j/J starts at top; k/K starts at bottom
            if self._selected_row == -1:
                new_idx = 0 if delta > 0 else max_idx
            else:
                new_idx = max(0, min(max_idx, self._selected_row + delta))
            self._set_selected_row(new_idx)
            return
        try:
            scroller = self.query_one("#main-content", VerticalScroll)
        except Exception:
            return
        scroller.scroll_relative(y=delta, animate=False)

    def _set_selected_row(self, new_idx: int) -> None:
        """Move call-log selection in place without rebuilding the panel.

        Updates the previously-selected row (if any) and the new one, then
        scrolls the new one into view. Falls back to a full rerender only
        when row widgets aren't mounted yet (first draw).
        """
        prev = self._selected_row
        self._selected_row = new_idx
        if not self._ops_row_specs:
            self._rerender_current()
            return
        try:
            rows = list(self.query(LogRow))
        except Exception:
            self._rerender_current()
            return
        if not rows:
            self._rerender_current()
            return
        # Update only the two rows whose state changed
        for idx in {prev, new_idx}:
            if 0 <= idx < len(rows) and idx < len(self._ops_row_specs):
                spec = self._ops_row_specs[idx]
                columns, row_class = self._render_row_spec(spec, selected=(idx == new_idx))
                rows[idx].update(columns)
                rows[idx].set_classes(row_class)
        self._refresh_selected_detail()
        if 0 <= new_idx < len(rows):
            with contextlib.suppress(Exception):
                rows[new_idx].scroll_visible(animate=False)

    def _rerender_current(self) -> None:
        """Re-render whichever view is active (board or fullscreen)."""
        if self._current_tab == BOARD:
            self._render_board()
        else:
            self._render_panel(self._current_tab)

    def action_expand_selected(self) -> None:
        """Show modal with full prompt + metadata for the selected entry.

        Works from the board and the fullscreen LOG (both show the call log).
        If nothing is selected, default to the top-most (most recent) entry.
        """
        if self._current_tab not in self._LOG_VIEWS or not self._ops_row_specs:
            return
        idx = self._selected_row if self._selected_row >= 0 else 0
        idx = max(0, min(len(self._ops_row_specs) - 1, idx))
        spec = self._ops_row_specs[idx]
        self.push_screen(EntryDetailScreen(spec.dt, spec.entry_id, spec.entry))

    def on_log_row_clicked(self, message: "LogRow.Clicked") -> None:
        """Clicking a log row selects it."""
        if self._current_tab not in self._LOG_VIEWS:
            return
        self._set_selected_row(message.row_index)

    def on_click(self, event: Click) -> None:
        """Clear call-log selection on clicks outside a log row."""
        if self._current_tab not in self._LOG_VIEWS:
            return
        w = getattr(event, "widget", None)
        while w is not None:
            if isinstance(w, LogRow):
                return
            w = getattr(w, "parent", None)
        if self._selected_row != -1:
            self._set_selected_row(-1)

    def action_board(self) -> None:
        """Return to the board from any fullscreen panel."""
        self._render_board()

    def _render_board(self) -> None:
        """Render the dense board as the active view."""
        self._current_tab = BOARD
        container = self.query_one("#panel-container", Vertical)
        scroller = self.query_one("#main-content", VerticalScroll)
        scroll_y = scroller.scroll_y
        container.remove_children()
        container.mount(self._build_hud())
        self._apply_hud_density()
        self._update_status_bar()
        self.call_after_refresh(lambda: scroller.scroll_to(y=scroll_y, animate=False))

    _PANEL_BUILDERS = {
        "TREND":   "_build_trend",
        "HEATMAP": "_build_heatmap",
        "LOG":     "_build_log_view",
        "CALLS":   "_build_cost_histogram",
    }

    def action_expand(self, panel: str) -> None:
        """Expand one panel to fullscreen."""
        if panel not in self._PANEL_BUILDERS:
            return
        self._render_panel(panel)

    def _render_panel(self, panel: str) -> None:
        self._current_tab = panel
        builder = getattr(self, self._PANEL_BUILDERS[panel])
        container = self.query_one("#panel-container", Vertical)
        container.remove_children()
        container.mount(builder())
        self._update_status_bar()

    def _build_log_view(self) -> Widget:
        """Fullscreen call log."""
        entries = self._snapshot.ops_entries if self._snapshot else []
        return Vertical(
            self._build_log_panel(entries),
            classes="chart-panel chart-stack",
        )

    def action_toggle_hourly_metric(self) -> None:
        """Cycle the board hourly bar between cost / tokens / requests.

        No-op off the board. Sticky for the session.
        """
        if self._current_tab != BOARD:
            return
        self._hourly_metric = HOURLY_CYCLE[self._hourly_metric]
        self._render_board()

    def action_toggle_trend_view(self) -> None:
        """Switch the existing trend panel between daily and model views."""
        if self._current_tab not in {BOARD, "TREND"}:
            return
        self._trend_view = "models" if self._trend_view == "daily" else "daily"
        if self._current_tab == BOARD:
            self._render_board()
        else:
            self._render_panel("TREND")

    def action_toggle_metric(self) -> None:
        """Unified m-key metric toggle.

        Board: switches summaries, charts, hourly activity, projects and call log.
        TREND models: switches the overall price / total tokens setting.
        TREND daily fullscreen: cost → tokens_io → tokens_cache → savings → cost
        HEATMAP fullscreen: cost → requests → tokens → cost
        No-op on LOG / CALLS.
        """
        tab = self._current_tab
        if tab == BOARD or (tab == "TREND" and self._trend_view == "models"):
            self._board_metric = (
                "tokens" if self._board_metric == "cost" else "cost"
            )
            self._hourly_metric = self._board_metric
            if tab == BOARD:
                self._render_board()
            else:
                self._render_panel("TREND")
        elif tab == "TREND":
            self._trend_metric = TREND_CYCLE[self._trend_metric]
            self._render_panel("TREND")
        elif tab == "HEATMAP":
            self._heatmap_metric = HEATMAP_CYCLE[self._heatmap_metric]
            self._render_panel("HEATMAP")



    @staticmethod
    def _init_plt(plot: PlotextPlot):
        """Reset a PlotextPlot and return its plt handle."""
        plt = plot.plt
        plt.clear_data()
        plt.clear_figure()
        plt.theme("dark")
        plt.plot_size(None, None)
        return plt

    @staticmethod
    def _set_yticks(plt, values: list, formatter, num_ticks: int = 5,
                    yside: str = "left") -> None:
        """Set Y-axis ticks with custom formatted labels."""
        if not values or max(values) == 0:
            return
        step = max(values) / num_ticks
        positions = [step * i for i in range(num_ticks + 1)]
        labels = [formatter(v) for v in positions]
        plt.yticks(positions, labels, yside=yside)

    @staticmethod
    def _set_date_xticks(plt, sorted_days: list[str], dates: list[int],
                         max_ticks: int = 10) -> None:
        if not sorted_days or not dates:
            return
        # When there are fewer days than ticks, emit one tick per day so the
        # axis still renders labels (an empty tick list makes plotext drop
        # the whole x-axis row — that's what blanked the daily-cost x-axis).
        tick_step = max(1, len(sorted_days) // max_ticks)
        tick_positions = dates[::tick_step] or list(dates)
        tick_labels = [sorted_days[i][5:] for i in tick_positions]
        plt.xticks(tick_positions, tick_labels)

    @staticmethod
    def _chart_panel(title: str, plot: Widget,
                     subtitle: str = "") -> Widget:
        """Standard wrapper: title + optional subtitle + plot, LCARS chart-panel."""
        children: list[Widget] = [Label(f"  {title}", classes="chart-title")]
        if subtitle:
            children.append(Label(f"  {subtitle}", classes="chart-subtitle"))
        children.append(plot)
        return Vertical(*children, classes="chart-panel")

    def _get_hm_grid(self, metric: str) -> list:
        """Cache reductions until a reload changes any record or filter."""
        if metric not in self._hm_grids:
            self._hm_grids[metric] = hour_week_grid(self._ledger, None, metric)
        return self._hm_grids[metric]

    def _heatmap_view(self, metric: str, max_ticks: int = 6) -> tuple:
        """Trim idle edge hours, preserving real clock labels and internal gaps.

        Use request activity for every metric so unpriced calls still count
        and switching units does not move the time axis. Empty data keeps 24h.
        """
        activity = self._get_hm_grid("requests")
        hours = [hour for hour in range(24) if any(row[hour] for row in activity)]
        first, last = (hours[0], hours[-1]) if hours else (0, 23)
        grid = [row[first:last + 1] for row in self._get_hm_grid(metric)]
        count = min(max_ticks, last - first + 1)
        columns = [round(i * (last - first) / max(1, count - 1)) for i in range(count)]
        ticks = [(column, f"{first + column:02d}") for column in columns]
        return grid, ticks

    def _chart_days(self, default_days: int) -> list:
        now = self._snapshot.clock.now if self._snapshot else datetime.now()
        end = min(now, datetime.fromisoformat(self._date_to)) if self._date_to else now
        days = default_days if self._date_from or self._date_to else (self._days or default_days)
        if self._date_from:
            days = max(1, (end.date() - datetime.fromisoformat(self._date_from).date()).days + 1)
        return calendar_days(end, days)

    def _make_chart(self, title: str, draw_fn: Callable,
                    subtitle: str = "") -> Widget:
        """Wrap a plotext draw function in the standard chart-panel scaffolding.

        `draw_fn(plt)` is deferred until the PlotextPlot widget is mounted.
        It owns plotting + axis setup; this helper owns init, refresh, and
        panel wrapping.
        """
        return self._chart_panel(title, self._make_plot(draw_fn), subtitle)

    def _make_plot(self, draw_fn: Callable) -> PlotextPlot:
        """Use the same mounted plot setup for board and fullscreen charts."""
        plot = PlotextPlot()
        def on_mount_chart():
            plt = self._init_plt(plot)
            draw_fn(plt)
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return plot

    # ── HUD — dense single-screen view ──────────────────────────────────
    #
    # Layout (top → bottom):
    #   1. KPI strip  — 6 summaries with sparklines
    #   2. Mid tier   — trend chart (left) + hour×weekday heatmap (right)
    #   3. Hourly bar — 24-hour bar in the selected metric
    #   4. Ranking    — projects / models / stops side-by-side
    #   5. Call log   — fills remaining height
    #
    # The mid tier is hidden when terminal height < HUD_COMPACT_ROWS.
    # All live cells bind to the snapshot; the chart + heatmap rebuild
    # when records change or the date rolls over (same gate as TREND).

    def _build_hud_kpi_cell(self, label: str, accent: str,
                             value_selector, detail_selector,
                             spark_selector=None) -> Vertical:
        """Compact KPI cell for the board strip: label · value · detail,
        plus a 1-row sparkline when a `spark_selector` is given."""
        cls = "hud-kpi-cell"
        if accent:
            cls += f" hud-kpi-cell-{accent}"
        children: list[Widget] = [
            Label(f" [#9999CC]{label}[/]",
                  classes="hud-kpi-label", markup=True),
            LiveLabel(
                self._metrics,
                lambda s: f" [#FF9900]{value_selector(s)}[/]",
                classes="hud-kpi-value",
            ),
            LiveLabel(
                self._metrics,
                lambda s: f" [dim]{detail_selector(s)}[/]",
                classes="hud-kpi-detail",
            ),
        ]
        if spark_selector is not None:
            children.append(LiveSparkline(
                self._metrics, spark_selector, summary_function=max,
                classes="hud-kpi-spark",
            ))
        return Vertical(*children, classes=cls)

    def _build_hud(self) -> Widget:
        """Dense HUD: KPI strip + charts + hourly + ranking + call log."""
        snap = self._snapshot
        token_mode = self._board_metric == "tokens"
        unit = "TOKENS" if token_mode else "PRICE"
        fmt = (lambda value: format_tokens(int(value))) if token_mode else format_cost
        periods = lambda s: getattr(s.overview, self._board_metric)
        overview = lambda s: s.overview
        kpi_strip = Horizontal(
            self._build_hud_kpi_cell(
                f"TODAY · {unit}", "",
                value_selector=lambda s: fmt(periods(s).today),
                detail_selector=lambda s: f"{format_number(overview(s).today_requests)} req",
                spark_selector=lambda s: periods(s).spark_7d,
            ),
            self._build_hud_kpi_cell(
                "WEEK", "alt",
                value_selector=lambda s: fmt(periods(s).week),
                detail_selector=lambda s: periods(s).week_change.replace(
                    " vs prior WTD", " WTD",
                ).replace("no prior WTD activity", "no prior week"),
                spark_selector=lambda s: periods(s).spark_4w,
            ),
            self._build_hud_kpi_cell(
                "30-DAY", "accent",
                value_selector=lambda s: fmt(periods(s).month),
                detail_selector=lambda s: f"{format_number(overview(s).month_requests)} req",
                spark_selector=lambda s: periods(s).spark_30d,
            ),
            self._build_hud_kpi_cell(
                "TOKENS 7d", "",
                value_selector=lambda s: format_tokens(int(sum(overview(s).tokens.spark_7d))),
                detail_selector=lambda s: (
                    f"{format_number(overview(s).tokens_7d_in)} in · "
                    f"{format_number(overview(s).tokens_7d_out)} out"
                ),
                spark_selector=lambda s: overview(s).tokens.spark_7d,
            ),
            self._build_hud_kpi_cell(
                "CACHE", "alt",
                value_selector=lambda s: (overview(s).cache_hit_label if token_mode
                                          else overview(s).cache_eff_label),
                detail_selector=lambda s: (
                    f"{format_tokens(overview(s).cache_reads_30d)} read 30d" if token_mode
                    else f"saved {format_cost(overview(s).cache_savings_30d)} 30d"
                ),
                spark_selector=lambda s: (overview(s).spark_7d_cache_hits if token_mode
                                          else overview(s).spark_7d_cache),
            ),
            self._build_hud_kpi_cell(
                "BURN", "accent",
                value_selector=lambda s: f"{fmt(periods(s).burn_rate)}/day",
                detail_selector=lambda s: (
                    f"{fmt(s.ops.tokens_per_hr if token_mode else s.ops.rate_per_hr)}/hr"
                ),
                spark_selector=lambda s: periods(s).spark_burn,
            ),
            id="hud-kpi-strip",
        )
        # ── 2. Mid tier: trend chart + hour×weekday heatmap ──
        # Both views switch together between query price and token volume.
        board_metric = self._board_metric
        next_metric = "price" if board_metric == "tokens" else "tokens"
        trend_widget = self._build_board_trend()

        # Heatmap: matching price/tokens activity by hour × weekday.
        hm_grid, hour_ticks = self._heatmap_view(board_metric)
        _czero, _clow, _chigh = HM_COLORS[board_metric]
        heatmap_widget = HeatmapGrid(
            hm_grid,
            y_labels=DAY_NAMES,
            x_labels=hour_ticks,
            color_zero=_czero,
            color_low=_clow,
            color_high=_chigh,
        )
        heatmap_panel = Vertical(
            heatmap_widget,
            classes="chart-panel",
            id="hud-heatmap",
        )
        heatmap_panel.border_title = (
            "◖ TOKENS BY HOUR × WEEKDAY ◗"
            if board_metric == "tokens"
            else "◖ PRICE BY HOUR × WEEKDAY ◗"
        )
        heatmap_panel.border_subtitle = (
            fr"\[m] → {next_metric} · \[2] full heatmap"
        )


        mid_tier = Horizontal(trend_widget, heatmap_panel, id="hud-mid")

        # ── 3. Hourly bar ──
        hourly_wrap = self._build_hourly_wrap()
        hourly_wrap.id = "hud-hourly"

        # ── 4. Ranking panels (today's projects + models + stops) ──
        ranking_row: Optional[Widget] = None
        if snap is not None:
            ranking_row = self._build_ranking_panels(snap.ops)
        if ranking_row is not None:
            ranking_row.id = "hud-ranking"

        # ── 5. Call log ──
        entries = snap.ops_entries if snap else []
        log_table = self._build_log_panel(entries)
        log_table.id = "hud-log-table"
        log_panel = Horizontal(
            log_table,
            self._build_selected_detail_panel(),
            id="hud-log",
        )

        children: list[Widget] = [
            kpi_strip, mid_tier, hourly_wrap,
        ]
        if ranking_row is not None:
            children.append(ranking_row)
        children.append(log_panel)

        return Vertical(*children, classes="chart-panel chart-stack")

    def _build_board_trend(self) -> Widget:
        """The board's trend panel, with its selected view on the same canvas."""
        if self._trend_view == "models":
            return self._build_model_trend(fullscreen=False)
        sorted_days = self._chart_days(30) if self._daily else []
        dates = list(range(len(sorted_days)))
        token_mode = self._board_metric == "tokens"
        unit = "TOKENS" if token_mode else "PRICE"
        color = (153, 153, 204) if token_mode else (255, 153, 0)
        fmt = (lambda v: format_tokens(int(v))) if token_mode else format_cost
        values = [
            total_tokens(self._daily.get(d, {})) if token_mode
            else self._daily.get(d, {}).get(FIELD_COST, 0)
            for d in sorted_days
        ]

        def draw_fn(plt):
            plt.plot(dates, values, marker="dot", color=color)
            self._set_date_xticks(plt, sorted_days, dates, max_ticks=6)
            self._set_yticks(plt, values, fmt)

        plot = self._make_plot(draw_fn) if sorted_days else Label("  No data")
        # Border captions keep the plot canvas tall enough for its date axis.
        panel = Vertical(plot, classes="chart-panel", id="hud-trend")
        panel.border_title = f"◖ DAILY {unit} — {len(sorted_days) or 30}d ◗"
        total = f"Total: {fmt(sum(values))} · " if sorted_days else ""
        panel.border_subtitle = total + r"\[t] models · \[1] full"
        return panel

    # ── TREND tab ───────────────────────────────────────────────────────

    #
    # Daily time-series. `m` cycles four views:
    #   cost       — 60d line chart of daily spend
    #   tokens_io  — 30d grouped bars: input + output
    #   tokens_cache — 30d grouped bars: cache writes + reads
    #   savings    — 60d dual-axis: cache savings ($) + efficiency (%)

    def _build_trend(self) -> Widget:
        if self._trend_view == "models":
            return self._build_model_trend(fullscreen=True)
        metric = self._trend_metric
        next_m = TREND_CYCLE[metric]


        if metric == "cost":
            sorted_days = (self._chart_days(60) if self._daily else [])
            if not sorted_days:
                return self._chart_panel("No data", Label(""))
            dates = list(range(len(sorted_days)))
            costs = [self._daily.get(d, {}).get(FIELD_COST, 0) for d in sorted_days]
            total_cost = sum(costs)

            def draw_fn(plt):
                plt.plot(dates, costs, marker="dot", color=(255, 153, 0))
                self._set_date_xticks(plt, sorted_days, dates)
                self._set_yticks(plt, costs, format_cost)

            title = f"DAILY COST — {len(sorted_days)}d ($)"
            subtitle = (
                f"{sorted_days[0]} → {sorted_days[-1]}  ◥  "
                f"Total: {format_cost(total_cost)}  ◥  "
                f"Avg: {format_cost(total_cost / len(costs))}/day  ◥  "
                f"[dim]\\[m] → {next_m}[/]"
            )

        elif metric == "tokens_io":
            sorted_days = (self._chart_days(30) if self._daily else [])
            if not sorted_days:
                return self._chart_panel("No data", Label(""))
            labels = [d[5:] for d in sorted_days]
            tokens_in = [self._daily.get(d, {}).get(FIELD_TOKENS_IN, 0) for d in sorted_days]
            tokens_out = [self._daily.get(d, {}).get(FIELD_TOKENS_OUT, 0) for d in sorted_days]

            def _fmt_tok(v):
                return format_tokens(int(v), compact=True)

            def draw_fn(plt):
                plt.multiple_bar(
                    labels,
                    [tokens_in, tokens_out],
                    labels=["Input", "Output"],
                    color=[(255, 153, 0), (204, 102, 153)],
                )
                self._set_yticks(plt, tokens_in + tokens_out, _fmt_tok)

            title = f"UNCACHED INPUT & OUTPUT — {len(sorted_days)}d"
            subtitle = (
                f"In: {format_tokens(sum(tokens_in))}  ◥  "
                f"Out: {format_tokens(sum(tokens_out))}  ◥  "
                f"[dim]\\[m] → {next_m}[/]"
            )

        elif metric == "tokens_cache":
            sorted_days = (self._chart_days(30) if self._daily else [])
            if not sorted_days:
                return self._chart_panel("No data", Label(""))
            labels = [d[5:] for d in sorted_days]
            cache_w = [self._daily.get(d, {}).get(FIELD_CACHE_WRITES, 0) for d in sorted_days]
            cache_r = [self._daily.get(d, {}).get(FIELD_CACHE_READS, 0) for d in sorted_days]

            def _fmt_tok(v):
                return format_tokens(int(v), compact=True)

            def draw_fn(plt):
                plt.multiple_bar(
                    labels,
                    [cache_w, cache_r],
                    labels=["Cache Write", "Cache Read"],
                    color=[(153, 153, 204), (204, 153, 204)],
                )
                self._set_yticks(plt, cache_w + cache_r, _fmt_tok)

            title = f"CACHE TOKENS — WRITES & READS ({len(sorted_days)}d)"
            subtitle = (
                f"Writes: {format_tokens(sum(cache_w))}  ◥  "
                f"Reads: {format_tokens(sum(cache_r))}  ◥  "
                f"[dim]\\[m] → {next_m}[/]"
            )

        else:  # savings
            sorted_days = (self._chart_days(60) if self._daily else [])
            if not sorted_days:
                return self._chart_panel("No data", Label(""))
            dates = list(range(len(sorted_days)))
            savings = [self._daily.get(d, {}).get(FIELD_CACHE_SAVINGS, 0) for d in sorted_days]
            costs = [self._daily.get(d, {}).get(FIELD_COST, 0) for d in sorted_days]
            pcts = [
                (s / (s + c) * 100) if (s + c) > 0 else 0.0
                for s, c in zip(savings, costs)
            ]
            total_saved = sum(savings)

            def draw_fn(plt):
                plt.plot(dates, savings, marker="dot", label="Savings ($)",
                         color=(153, 153, 204))
                plt.plot(dates, pcts, marker="dot", label="Efficiency (%)",
                         color=(255, 153, 0), yside="right")
                self._set_date_xticks(plt, sorted_days, dates)
                self._set_yticks(plt, savings, format_cost, yside="left")
                self._set_yticks(plt, pcts, lambda v: f"{v:.0f}%", yside="right")

            title = f"CACHE PERFORMANCE — SAVINGS & EFFICIENCY ({len(sorted_days)}d)"
            avg_eff = sum(pcts) / len(pcts) if pcts else 0.0
            subtitle = (
                f"Total saved: {format_cost(total_saved)}  ◥  "
                f"Avg efficiency: {avg_eff:.0f}%  ◥  "
                f"[dim]\\[m] → {next_m}[/]"
            )

        subtitle += r" · \[t] models 24h"
        return self._make_chart(title, draw_fn, subtitle)

    def _build_model_trend(self, fullscreen: bool) -> Widget:
        """One hourly line per recorded model, using the overall board units."""
        now = self._snapshot.clock.now if self._snapshot else datetime.now()
        hours, series = hourly_by_model(self._ledger, self._board_metric, now)
        token_mode = self._board_metric == "tokens"
        unit = "TOKENS" if token_mode else "PRICE"
        fmt = (lambda v: format_tokens(int(v))) if token_mode else format_cost
        next_unit = "price" if token_mode else "tokens"
        title = f"MODEL {unit} — 24h"
        model_totals = {model: sum(values) for model, values in series.items()}
        total = sum(model_totals.values())
        empty_text = "  No model usage in the last 24 hours"
        if series and not total:
            empty_text = (
                "  No model token usage in the last 24 hours" if token_mode
                else "  No model cost in the last 24 hours"
            )
        series = {
            model: series[model]
            for model in sorted(series, key=lambda model: (-model_totals[model], model))
            if model_totals[model] > 0
            and model_totals[model] >= total * MODEL_TREND_MIN_SHARE
        }
        if total > 0 and not series:
            empty_text = "  No model contributes at least 1% of the total"
        subtitle = f"{fmt(total)} total · hourly"
        if fullscreen:
            subtitle += fr" · \[m] {next_unit} · \[t] daily · esc board"
        else:
            subtitle += r" · \[t] daily · \[1] full"

        # Preserve distinct versions/unknown IDs even when short_model uses
        # the same display name. Rank colors separate the leading four models.
        names = {model: short_model(model) for model in series}
        colors = {
            model: MODEL_TREND_COLORS[i % len(MODEL_TREND_COLORS)]
            for i, model in enumerate(series)
        }
        positions = list(range(len(hours)))

        def draw_fn(plt):
            for model, values in series.items():
                name = names[model]
                label = model if list(names.values()).count(name) > 1 else name
                # Missing points break the line across idle hours.
                visible_values = [value if value != 0 else None for value in values]
                plt.plot(positions, visible_values, marker="dot", label=label, color=colors[model])
            ticks = list(range(0, len(hours), 6))
            plt.xticks(ticks, [hours[i].strftime("%H:%M") for i in ticks])
            plt.xlim(0, len(hours) - 1)
            peak = max((max(values) for values in series.values()), default=0) or 1
            self._set_yticks(plt, [peak], fmt)
            plt.ylim(0, peak)

        if fullscreen:
            if not series:
                return self._chart_panel(title, Label(empty_text), subtitle)
            return self._make_chart(title, draw_fn, subtitle)

        plot = self._make_plot(draw_fn) if series else Label(empty_text)
        panel = Vertical(plot, classes="chart-panel", id="hud-trend")
        panel.border_title = f"◖ {title} ◗"
        panel.border_subtitle = subtitle
        return panel

    # ── HEATMAP tab (hour × weekday, metric toggle) ──────────────────────
    #
    # `m` cycles cost → requests → tokens.
    # Each metric uses its own color ramp so the visual language stays
    # consistent with the rest of the app (amber=cost, mauve=requests,
    # periwinkle=tokens).

    def _build_heatmap(self) -> Widget:
        metric = self._heatmap_metric
        next_m = HEATMAP_CYCLE[metric]


        # Use memoized grid — avoids re-scanning the full ledger on metric toggle.
        grid = self._get_hm_grid(metric)
        flat = [v for row in grid for v in row if v > 0]
        total_v = sum(v for row in grid for v in row)

        color_zero, color_low, color_high = HM_COLORS.get(metric, HM_COLORS["cost"])

        if flat:
            peak_val = max(flat)
            peak_day, peak_hour = next(
                (d, h) for d in range(7) for h in range(24)
                if grid[d][h] == peak_val
            )
            if metric == "cost":
                peak_label = (
                    f"{DAY_NAMES[peak_day]} {peak_hour:02d}:00 "
                    f"({format_cost(peak_val)})"
                )
                total_label = format_cost(total_v)
                title = "COST BY HOUR × WEEKDAY"
            elif metric == "requests":
                peak_label = (
                    f"{DAY_NAMES[peak_day]} {peak_hour:02d}:00 "
                    f"({int(peak_val):,} req)"
                )
                total_label = f"{int(total_v):,} req"
                title = "REQUESTS BY HOUR × WEEKDAY"
            else:  # tokens
                peak_label = (
                    f"{DAY_NAMES[peak_day]} {peak_hour:02d}:00 "
                    f"({format_tokens(int(peak_val), compact=True)})"
                )
                total_label = format_tokens(int(total_v))
                title = "TOKENS BY HOUR × WEEKDAY"
        else:
            peak_label = "—"
            total_label = "—"
            title = "HEATMAP — BY HOUR × WEEKDAY"

        display_grid, hour_ticks = self._heatmap_view(metric, max_ticks=9)
        heatmap = HeatmapGrid(
            display_grid,
            y_labels=DAY_NAMES,
            x_labels=hour_ticks,
            color_zero=color_zero,
            color_low=color_low,
            color_high=color_high,
        )

        subtitle = (
            f"Peak: {peak_label}  ◥  "
            f"Total: {total_label}  ◥  "
            f"[dim]\\[m] → {next_m}[/]"
        )
        return self._chart_panel(title, heatmap, subtitle)

    # ── Session cost histogram ──

    def _build_cost_histogram(self) -> Widget:
        """Per-call cost distribution.

        Layout matches the rest of the LCARS dashboard:
          1. Header SESSION-STATS-style stat row (CALLS / TOTAL / MEDIAN /
             MEAN / P95 / P99) in an ``ops-panel-session``.
          2. Bucket panel with one ranked-panel-style row per log-spaced
             bucket. Each row uses a ``FluidBar`` so the histogram
             auto-sizes to terminal width.

        Percentile flags live to the right of the bar (next to the LCARS
        end cap), keeping the bar geometry clean.
        """
        costs = []
        for _dt, entry in iter_individual_entries(
                self._ledger, self._source_filter):
            c = entry.get(FIELD_COST) or 0
            if c > 0:
                costs.append(c)

        if not costs:
            return self._chart_panel("No cost data", Label(""))

        costs.sort()
        median = costs[len(costs) // 2]
        p95 = costs[int(len(costs) * 0.95)]
        p99 = costs[int(len(costs) * 0.99)]
        mean = sum(costs) / len(costs)
        total_calls = len(costs)
        total_cost = sum(costs)

        # Log-spaced buckets covering typical API call cost range
        bucket_edges = [0, 0.001, 0.005, 0.01, 0.05, 0.10, 0.25, 0.50,
                        1.00, 2.50, 5.00, float('inf')]
        bucket_labels = [
            "< $0.001", "$0.001–$0.005", "$0.005–$0.01",
            "$0.01–$0.05", "$0.05–$0.10", "$0.10–$0.25", "$0.25–$0.50",
            "$0.50–$1.00", "$1.00–$2.50", "$2.50–$5.00", "> $5.00",
        ]
        bucket_counts = [0] * len(bucket_labels)
        bucket_totals = [0.0] * len(bucket_labels)
        for c in costs:
            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= c < bucket_edges[i + 1]:
                    bucket_counts[i] += 1
                    bucket_totals[i] += c
                    break

        max_count = max(bucket_counts) if bucket_counts else 1

        # Percentile positions mapped to bucket index
        def bucket_of(c: float) -> int:
            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= c < bucket_edges[i + 1]:
                    return i
            return len(bucket_counts) - 1
        median_b = bucket_of(median)
        p95_b = bucket_of(p95)
        p99_b = bucket_of(p99)

        # Distribution summary.
        header_row = Horizontal(
            self._build_recent_stat(
                "CALLS", "",
                value=f"{total_calls:,}",
                detail="non-zero cost",
            ),
            self._build_recent_stat(
                "TOTAL", "alt",
                value=format_cost(total_cost),
                detail=f"avg {format_cost(mean)}",
            ),
            self._build_recent_stat(
                "MEDIAN", "accent",
                value=format_cost(median),
                detail="50th pct",
            ),
            self._build_recent_stat(
                "P95", "",
                value=format_cost(p95),
                detail="95th pct",
            ),
            self._build_recent_stat(
                "P99", "alt",
                value=format_cost(p99),
                detail="99th pct",
            ),
            self._build_recent_stat(
                "MAX", "accent",
                value=format_cost(costs[-1]),
                detail=f"min {format_cost(costs[0])}",
            ),
            classes="ops-stat-row",
        )
        header_panel = Vertical(
            header_row, classes="ops-panel ops-panel-session",
        )
        header_panel.border_title = "◖ COST DISTRIBUTION ◗"
        header_panel.border_subtitle = (
            f"per API call · {total_calls:,} calls · {format_cost(total_cost)} total"
        )

        # Bucket rows — one FluidBar each, with percentile flag suffixes.
        bucket_children: list[Widget] = [Label(
            f"  [b]{'BUCKET':<16}{'COUNT':>8}{'SHARE':>8}[/b]   "
            f"DISTRIBUTION                              "
            f"[b]{'COST SUM':>10}[/b]",
            classes="ops-log-header", markup=True,
        )]
        for i, (lbl, count, tot) in enumerate(
            zip(bucket_labels, bucket_counts, bucket_totals)
        ):
            share = count / total_calls * 100 if total_calls else 0
            fraction = (count / max_count) if max_count else 0
            cost_str = format_cost(tot) if tot > 0 else "—"

            # Bar fill color escalates with bucket cost — periwinkle for
            # cheap buckets, mauve mid-range, amber for the tail. Matches
            # the cost-warmth gradient used by the SPEND HEATMAP.
            if i <= 2:
                fill = "#9999CC"
            elif i <= 6:
                fill = "#CC6699"
            else:
                fill = "#FF9900"

            flag = ""
            if i == median_b:
                flag = " [#9999CC]◀ med[/]"
            if i == p95_b:
                flag = (flag + " [#CC6699]◀ p95[/]") if flag else " [#CC6699]◀ p95[/]"
            if i == p99_b:
                flag = (flag + " [#FF9900]◀ p99[/]") if flag else " [#FF9900]◀ p99[/]"

            bucket_children.append(Horizontal(
                Label(
                    f" [#FFCC99]{lbl:<16}[/] [#FF9900]{count:>7,}[/] "
                    f"[dim]{share:>6.1f}%[/] ",
                    classes="calls-bucket-label", markup=True,
                ),
                Static("[#CC6699]◖[/]", classes="bar-cap", markup=True),
                FluidBar(fraction, fill_color=fill, classes="fluid-bar"),
                Static("[#CC6699]◗[/]", classes="bar-cap", markup=True),
                Label(
                    f"{flag} [#CC9966]{cost_str:>10}[/]",
                    classes="calls-bucket-suffix", markup=True,
                ),
                classes="calls-bucket-row",
            ))

        bucket_children.append(Label(
            " [dim]Buckets are log-spaced; most calls cluster at the low end. "
            "Percentile markers (◀) flag where the median, P95, and P99 fall.[/]",
            classes="ops-kv-line", markup=True,
        ))

        bucket_panel = Vertical(
            *bucket_children, classes="ops-panel ops-panel-fill",
        )
        bucket_panel.border_title = "◖ BUCKETS ◗"
        bucket_panel.border_subtitle = (
            f"log-spaced · {len(bucket_labels)} ranges"
        )

        return Vertical(
            header_panel, bucket_panel,
            classes="chart-panel chart-stack",
        )

    def _build_recent_stat(self, label: str, accent: str,
                           value: str, detail: str) -> Vertical:
        """Static stat cell for a stat-band row (used by the CALLS header).

        Not bound to LiveLabel — the histogram rebuilds on expand, so a
        plain string is fine and avoids subscribing watchers per cell.
        """
        cls = "ops-stat-cell"
        if accent:
            cls += f" ops-stat-cell-{accent}"
        return Vertical(
            Label(f" [#9999CC]{label}[/]",
                  classes="ops-stat-cell-label", markup=True),
            Label(f" [#FF9900]{value}[/]",
                  classes="ops-stat-cell-value", markup=True),
            Label(f" [dim]{detail}[/]",
                  classes="ops-stat-cell-detail", markup=True),
            classes=cls,
        )

    # ── Call log refresh ──

    def _refresh_log_rows(self) -> None:
        """Re-render call-log rows from the current snapshot.

        Row markup depends on per-row state (selected, is_new) that isn't
        captured by the snapshot, so each tick we regenerate specs and
        mutate rows in place. A row-count change triggers a full tab
        rebuild — that's a structural edit the mounted widgets can't
        absorb.
        """
        snap = self._snapshot
        if snap is None:
            return
        entries = snap.ops_entries

        specs = build_row_specs(entries, now=self._snapshot.clock.now)
        if len(specs) != len(self._ops_row_specs):
            self._rerender_current()
            return
        self._replace_row_specs(specs)
        try:
            rows = list(self.query(LogRow))
        except Exception:
            return
        if len(rows) != len(specs):
            self._rerender_current()
            return

        for idx, (row, spec) in enumerate(zip(rows, specs)):
            columns, row_class = self._render_row_spec(
                spec, selected=(idx == self._selected_row),
            )
            row.update(columns)
            row.set_classes(row_class)
        self._refresh_selected_detail()

    # ── Board panel builders ──

    # Hourly-bar metric → (selector, subtitle_fn, title_suffix)

    _HOURLY_METRICS = {
        "cost":     (lambda s: s.ops.hour_cost,
                     lambda s: f"cost/hr · today {format_cost(s.ops.today_cost)}",
                     "COST ($)"),
        "tokens":   (lambda s: s.ops.hour_tokens,
                     lambda s: f"tokens/hr · today {format_tokens(int(sum(s.ops.hour_tokens)))}",
                     "TOKENS"),
        "requests": (lambda s: s.ops.hour_requests,
                     lambda s: f"calls/hr · today {int(sum(s.ops.hour_requests)):,}",
                     "REQUESTS"),
    }

    def _build_hourly_wrap(self) -> Widget:
        """24-cell hourly bar + tick axis, bound to snapshot.

        `h` cycles the metric between cost, tokens, and requests.
        """
        metric = self._hourly_metric
        selector, subtitle_fn, suffix = self._HOURLY_METRICS[metric]
        next_metric = HOURLY_CYCLE[metric]


        spark = LiveHourlyBar(
            self._metrics,
            selector,
            classes="ops-hourly-spark",
        )
        axis_cells: list[Widget] = []
        for h in range(24):
            lbl = f"{h:02d}" if (h % 6 == 0 or h == 23) else " "
            axis_cells.append(Static(
                f"[dim]{lbl}[/]", classes="hourly-axis", markup=True,
            ))
        axis = Horizontal(*axis_cells, classes="ops-hourly-axis")
        wrap = Vertical(spark, axis, classes="ops-hourly-wrap")
        wrap.border_title = f"◖ HOURLY ACTIVITY — {suffix} ◗"
        wrap.compose_add_child(LiveBorderSubtitle(
            self._metrics,
            lambda s: subtitle_fn(s) + f"  [dim]\\[h] → {next_metric}[/]",
            parent_widget=wrap,
        ))
        return wrap

    def _build_ranking_panels(self, view: OpsView) -> Optional[Widget]:
        """Build the Projects / Models / Stops / Subagents side-by-side row."""
        s = view.stats
        panels: list[Widget] = []

        project_key = "project_tokens" if self._board_metric == "tokens" else "project_cost"
        sorted_projects = sorted(s[project_key].items(),
                                 key=lambda x: x[1], reverse=True)
        if sorted_projects:
            panels.append(self._build_projects_panel(sorted_projects))

        if s["model_counts"]:
            panels.append(self._build_models_panel(s["model_counts"]))
        if s["stop_counts"]:
            panels.append(self._build_stops_panel(s["stop_counts"]))
        if s["subagent_type_counts"]:
            panels.append(self._build_subagents_panel(s["subagent_type_counts"]))

        if not panels:
            return None
        return Horizontal(*panels, classes="ops-side-by-side")

    def _build_projects_panel(self, sorted_projects: list) -> Widget:
        top = sorted_projects[:TOP_N_PANEL]
        max_cost = max((c for _, c in top), default=1) or 1
        fmt = (lambda v: format_tokens(int(v), compact=True)) if self._board_metric == "tokens" else format_cost
        rows = [
            self._panel_row(proj, fmt(cost), cost / max_cost, "#FF9900")
            for proj, cost in top
        ]
        return self._ranked_panel(
            "ACTIVE PROJECTS", f"{len(sorted_projects)} total · {self._board_metric}", rows,
        )

    def _build_models_panel(self, model_counts) -> Widget:
        segments = sorted(model_counts.items(),
                          key=lambda x: x[1], reverse=True)
        max_count = segments[0][1]
        total = sum(model_counts.values()) or 1
        rows = [
            self._panel_row(m, f"{c:,}", c / max_count, model_color(m))
            for m, c in segments
        ]
        return self._ranked_panel("MODEL MIX", f"{total:,} calls", rows)

    def _build_stops_panel(self, stop_counts) -> Widget:
        ordered = sorted(stop_counts.items(),
                         key=lambda x: x[1], reverse=True)
        max_c = ordered[0][1]
        total = sum(stop_counts.values()) or 1
        rows = [
            self._panel_row(
                sr, f"{c:,}", c / max_c, _STOP_COLORS.get(sr, "#CC9966"),
            )
            for sr, c in ordered
        ]
        return self._ranked_panel("STOP REASONS", f"{total:,} turns", rows)

    def _build_subagents_panel(self, subagent_types) -> Widget:
        top = subagent_types.most_common(TOP_N_PANEL)
        max_c = top[0][1]
        total = sum(subagent_types.values()) or 1
        rows: list[Widget] = [
            self._panel_row(
                (t.split(":", 1)[-1] if ":" in t else t),
                f"{c:,}", c / max_c, "#CC99CC",
            )
            for t, c in top
        ]
        if len(subagent_types) > TOP_N_PANEL:
            hidden = len(subagent_types) - TOP_N_PANEL
            hidden_n = sum(c for _, c in subagent_types.most_common()[TOP_N_PANEL:])
            rows.append(Label(
                f" [dim]+ {hidden} more · {hidden_n:,} calls[/]",
                classes="panel-row-footer", markup=True,
            ))
        return self._ranked_panel(
            "SUBAGENT TYPES",
            f"{total:,} spawns · {len(subagent_types)} types",
            rows,
        )

    # ── Ranked-panel primitives ──

    _PANEL_LABEL_W = 18

    @staticmethod
    def _esc_markup(s: str) -> str:
        """Escape `[` so user data can't inject Textual markup."""
        return s.replace("[", r"\[")

    @classmethod
    def _panel_row(cls, label: str, value_str: str, fraction: float,
                   fill: str) -> Horizontal:
        """One unified ranked-panel row: label+value, elastic bar, end caps.

        Bars use max-normalization (leader = 100% wide) so visual contrast
        reflects *relative rank*, not absolute share.
        """
        w = cls._PANEL_LABEL_W
        safe_label = cls._esc_markup(label[:w])
        safe_value = cls._esc_markup(value_str)
        return Horizontal(
            Label(
                f" [#FFCC99]{safe_label:<{w}}[/] "
                f"[#FF9900]{safe_value:>7}[/] ",
                classes="panel-row-label", markup=True,
            ),
            Static("[#CC6699]◖[/]", classes="bar-cap", markup=True),
            FluidBar(fraction, fill_color=fill, classes="fluid-bar"),
            Static("[#CC6699]◗[/]", classes="bar-cap", markup=True),
            classes="ops-labeled-bar",
        )

    @staticmethod
    def _ranked_panel(title: str, subtitle: str, rows: List[Widget]) -> Vertical:
        panel = Vertical(*rows, classes="ops-panel ops-panel-third")
        panel.border_title = f"◖ {title} ◗"
        panel.border_subtitle = subtitle
        return panel

    # ── Call log panel ──

    # Header labels aligned to LOG_COLUMNS widths.
    # Each entry is the header text for that column (markup OK).
    _LOG_HEADER_COLS = [
        "",           # marker — blank
        "TIME",       # time
        "MODEL",      # model
        "IN",         # in
        "OUT",        # out
        "CACHE",      # cache
        "COST ·",     # cost (· = cost-bar label)
        "",           # bar — blank
        "↳",          # subagent marker
        "TOOLS",      # tools
        "PROJECT",    # project
        "ACTIVITY",   # activity
    ]

    def _replace_row_specs(self, specs: List[RowSpec]) -> None:
        selected_id = (self._ops_row_specs[self._selected_row].entry_id
                       if 0 <= self._selected_row < len(self._ops_row_specs) else None)
        self._ops_row_specs = specs
        if selected_id is not None:
            self._selected_row = next((i for i, row in enumerate(specs)
                                       if row.entry_id == selected_id), -1)

    def _build_log_panel(self, entries: list) -> Widget:
        """Recent-calls log panel — header + up to LOG_ROW_CAP rows."""
        specs = build_row_specs(entries, now=self._snapshot.clock.now)
        self._replace_row_specs(specs)

        if self._selected_row >= len(specs):
            self._selected_row = len(specs) - 1 if specs else -1

        # Header row: one Static per column, same widths as LogRow cells.
        header_cells: list[Widget] = []
        headers = list(self._LOG_HEADER_COLS)
        headers[6] = "TOKENS ·" if self._board_metric == "tokens" else "PRICE ·"
        for hdr, (css_cls, _w) in zip(headers, LOG_COLUMNS):
            header_cells.append(Static(
                f"[b #9999CC]{hdr}[/]" if hdr else "",
                classes=css_cls,
                markup=True,
            ))
        header = Horizontal(*header_cells, classes="ops-log-header")

        children: list[Widget] = [header]
        for idx, spec in enumerate(specs):
            columns, row_class = self._render_row_spec(
                spec, selected=(idx == self._selected_row),
            )
            children.append(LogRow(
                columns, row_index=idx, classes=row_class,
            ))

        panel = Vertical(*children, classes="ops-panel ops-panel-log")
        panel.border_title = "◖ CALL LOG ◗"
        panel.border_subtitle = f"{len(specs)} most recent"
        # Live subtitle: "N most recent" changes only on row-count flips,
        # which already trigger a tab rebuild — still, binding it keeps
        # subtitle/body in lockstep even if _refresh_log_rows shortcuts.
        panel.compose_add_child(LiveBorderSubtitle(
            self._metrics,
            lambda s: f"{min(sum(e.get('source') != 'agent_spawn' for _, _, e in s.ops_entries), LOG_ROW_CAP)} most recent",
            parent_widget=panel,
        ))
        return panel

    def _build_selected_detail_panel(self) -> Widget:
        """Persistent, compact detail view for the selected call-log row."""
        panel = Vertical(
            Static(
                self._selected_detail_markup(),
                id="query-detail-content",
                markup=True,
            ),
            id="hud-log-detail",
        )
        panel.border_title = "◖ SELECTED QUERY ◗"
        panel.border_subtitle = r"\[j/k] browse · \[enter] full detail"
        return panel

    def _refresh_selected_detail(self) -> None:
        """Update the board-only selected-query inspector if it is mounted."""
        try:
            detail = self.query_one("#query-detail-content", Static)
        except Exception:
            return
        detail.update(self._selected_detail_markup())

    def _selected_detail_markup(self) -> str:
        """Render the selected call's useful metadata in the board side pane."""
        if not self._ops_row_specs:
            return "[dim]No queries in the active filter.[/]"

        idx = self._selected_row if self._selected_row >= 0 else 0
        idx = min(idx, len(self._ops_row_specs) - 1)
        spec = self._ops_row_specs[idx]
        entry = spec.entry

        raw_model = str(entry.get("model") or "unknown")
        display_model = short_model(raw_model)
        source = SOURCE_DISPLAY.get(entry.get("source"), entry.get("source") or "unknown")
        surface = str(entry.get("surface") or "")
        provider = str(entry.get("modelProvider") or "")
        source_bits = [str(source)]
        if surface:
            source_bits.append(surface)
        if provider:
            source_bits.append(provider)

        own_cost = entry.get(FIELD_COST)
        display_cost = None if own_cost is None else own_cost + spec.spawn_cost
        pricing_ref = entry.get("pricingRef") or entry.get("costProvenance") or "unavailable"

        def token(value: object) -> str:
            try:
                return "—" if value is None else format_number(max(0, int(value)))
            except (TypeError, ValueError):
                return "—"

        prompt = str(entry.get("promptPreview") or "").strip().replace("\n", " ")
        if not prompt:
            prompt = "No prompt text stored."
        elif len(prompt) > 240:
            prompt = prompt[:237].rstrip() + "…"

        project = str(entry.get("project") or "—")
        tools = short_tools(entry.get("tools") or []) or "—"
        stop = str(entry.get("stopReason") or "—")
        reasoning = entry.get("reasoningTokens")

        lines = [
            f"[b {model_color(display_model)}]{self._esc_markup(display_model)}[/]"
            f" [dim]{self._esc_markup(raw_model)}[/]",
            f"[#9999CC]SOURCE[/] {self._esc_markup(' · '.join(source_bits))}",
            "",
            f"[#9999CC]QUERY PRICE[/] [b #FF9900]{format_cost(display_cost)}[/]",
            f"[#9999CC]TOTAL TOKENS[/] [b #FF9900]{format_tokens(total_tokens(entry) + spec.spawn_tokens)}[/]",
            f"[dim]{self._esc_markup(str(pricing_ref))}[/]",
            (
                f"[#9999CC]TOKENS[/] in {token(entry.get(FIELD_TOKENS_IN))}"
                f" · out {token(entry.get(FIELD_TOKENS_OUT))}"
            ),
            (
                f"[#9999CC]CACHE[/] read {token(entry.get(FIELD_CACHE_READS))}"
                f" · write {token(entry.get(FIELD_CACHE_WRITES))}"
            ),
        ]
        for assumption in entry.get("pricingAssumptions") or []:
            lines.append(f"[#FF9900]ASSUMPTION[/] {self._esc_markup(str(assumption))}")
        if reasoning is not None:
            lines.append(f"[#9999CC]REASONING[/] {token(reasoning)} [dim](included in output)[/]")
        if spec.spawn_cost > 0:
            lines.append(
                f"[#9999CC]SUBAGENTS[/] +{format_cost(spec.spawn_cost)} · "
                f"{format_tokens(spec.spawn_tokens)} included above"
            )

        lines.extend([
            "",
            f"[#9999CC]TIME[/] {spec.dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"[#9999CC]PROJECT[/] {self._esc_markup(project)}",
            f"[#9999CC]TOOLS[/] {self._esc_markup(tools)}",
            f"[#9999CC]STOP[/] {self._esc_markup(stop)}",
            "",
            "[#9999CC]PROMPT[/]",
            f"[#FFCC99]{self._esc_markup(prompt)}[/]",
        ])
        return "\n".join(lines)

    @staticmethod
    def _row_marker(spec: RowSpec, selected: bool, is_new: bool) -> str:
        """Priority ladder for the left-gutter marker glyph."""
        if selected:
            return "►"
        if spec.is_anchor:
            return "▶" if spec.is_turn_end else "◆"
        if is_new:
            return "★"
        return " "

    @staticmethod
    def _row_classes(spec: RowSpec, selected: bool, is_new: bool) -> str:
        """CSS class list for a log row. Mutually exclusive accents."""
        base = "ops-log-row"
        if selected:
            return base + " ops-log-row-selected"
        if is_new:
            return base + " ops-log-row-new"
        if not spec.is_anchor:
            return base + " ops-log-row-cont"
        if spec.is_subagent:
            return base + " ops-log-row-subagent"
        return base

    def _render_row_spec(self, spec: RowSpec, selected: bool) -> tuple:
        """Render a row spec into (List[str] columns, css_class_string).

        Each element of the returned list corresponds to one column in
        LOG_COLUMNS order. The caller passes the list directly to
        LogRow(columns=...) or LogRow.update(columns).
        """
        e = spec.entry
        dt = spec.dt
        short_m = short_model(e.get("model"))
        color = model_color(short_m)
        tok_in = format_number(e.get(FIELD_TOKENS_IN))
        tok_out = format_number(e.get(FIELD_TOKENS_OUT))
        cr = e.get(FIELD_CACHE_READS)
        cw = e.get(FIELD_CACHE_WRITES)
        cache_str = f"{format_number(cr)}/{format_number(cw)}"
        own_cost = e.get(FIELD_COST)
        display_cost = (
            None if own_cost is None else own_cost + spec.spawn_cost
        )
        if self._board_metric == "tokens":
            value = total_tokens(e) + spec.spawn_tokens
            cost = format_tokens(value, compact=True)
            bar = cost_bar(value, spec.max_log_tokens)
        else:
            cost = format_cost(display_cost)
            bar = cost_bar(display_cost or 0, spec.max_log_cost)
        proj = short_project(e.get("project", ""))[:14]
        kind_marker = "[#9999CC]↳[/]" if spec.is_subagent else ""
        time_str = dt.strftime("%H:%M:%S")
        tools_str = short_tools(e.get("tools") or [])[:8]

        activity = self._row_activity(spec)
        is_new = spec.entry_id in self._new_entry_ids
        marker = self._row_marker(spec, selected, is_new)
        row_classes = self._row_classes(spec, selected, is_new)

        # One string per LOG_COLUMNS entry (12 columns total).
        columns = [
            marker,                              # log-col-marker
            time_str,                            # log-col-time
            f"[{color}]{self._esc_markup(short_m)}[/]",            # log-col-model
            tok_in,                              # log-col-in
            tok_out,                             # log-col-out
            cache_str,                           # log-col-cache
            cost,                                # log-col-cost
            bar,                                 # log-col-bar
            kind_marker,                         # log-col-sub
            self._esc_markup(tools_str),          # log-col-tools
            self._esc_markup(proj),                                # log-col-project
            activity,                            # log-col-activity
        ]
        return columns, row_classes

    def _row_activity(self, spec: RowSpec) -> str:
        """Activity column text for a call-log row."""
        if spec.is_anchor:
            activity = row_activity_text(spec.entry)
            if spec.spawn_cost > 0:
                amount = (format_tokens(spec.spawn_tokens) if self._board_metric == "tokens"
                          else format_cost(spec.spawn_cost))
                activity += (f" [dim #9999CC](+{amount} "
                             f"subagents)[/]")
            return activity
        # Continuation rows: tool chain if present, else a quiet dot.
        if spec.entry.get("tools"):
            return row_activity_text(spec.entry)
        return "[dim]  ⋮[/]"


if __name__ == "__main__":
    app = CostTrackerApp()
    app.run()
