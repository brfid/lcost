"""Textual widgets used by the lcost OPS dashboard.

These are generic, stateless presentation components. They know how to render
themselves given data; they do not know about the ledger, aggregation, or the
app shell.
"""

import math
from typing import Callable, Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Label, Sparkline, Static

from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    format_cost,
)


# ── Fluid-width bars ──────────────────────────────────────────────────────

class _FluidBarBase(Static):
    """Base for bars that redraw when their container width changes."""

    def _draw(self, width: int) -> str:
        raise NotImplementedError

    def on_resize(self, event) -> None:
        self.update(self._draw(event.size.width))

    def on_mount(self) -> None:
        if self.size.width > 0:
            self.update(self._draw(self.size.width))


class FluidBar(_FluidBarBase):
    """Proportional horizontal fill bar; `fraction` ∈ [0, 1].

    If `label` is given and the filled portion has enough room (≥ len+2), the
    label is drawn inside the fill in `label_color`. Otherwise the bar renders
    plain and the caller can place a label next to it externally.
    """

    def __init__(self, fraction: float, fill_color: str = "#FF9900",
                 empty_color: str = "#3a3a4a", label: str = "",
                 label_color: str = "#000000", **kwargs):
        super().__init__("", **kwargs)
        self._fraction = max(0.0, min(1.0, fraction))
        self._fill_color = fill_color
        self._empty_color = empty_color
        self._label = label
        self._label_color = label_color

    def _draw(self, width: int) -> str:
        if width <= 0:
            return ""
        fill = max(0, min(width, int(self._fraction * width)))
        empty = width - fill
        fill_block = "█" * fill
        if self._label and fill >= len(self._label) + 2:
            # Center the label inside the fill
            pad = (fill - len(self._label)) // 2
            fill_block = (
                "█" * pad
                + self._label
                + "█" * (fill - pad - len(self._label))
            )
            return (
                f"[{self._label_color} on {self._fill_color}]{fill_block}[/]"
                f"[{self._empty_color}]{'░' * empty}[/]"
            )
        return (
            f"[{self._fill_color}]{fill_block}[/]"
            f"[{self._empty_color}]{'░' * empty}[/]"
        )


# ── Heatmap grid ──────────────────────────────────────────────────────────

class HeatmapGrid(Vertical):
    """LCARS-styled 2D heatmap rendered with native Textual cells.

    Each data row is a ``Horizontal`` with ``1fr`` height so the grid fills
    its container without gaps. Cells are rendered as solid background blocks
    (no glyph + content-align) so there are no centering gaps regardless of
    how the container distributes height.

    Layout:
      ┌─────────┬───────────────────────────────────────┐
      │         │ x-tick labels (month names, hours, …) │
      │ y-ticks │ row of 1fr cells per data row        │
      │         │ row of 1fr cells per data row        │
      │         │ …                                    │
      └─────────┴───────────────────────────────────────┘

    Parameters:
      grid: ``rows × cols`` floats. Higher = more intense.
      y_labels: One label per row (top-to-bottom).
      x_labels: ``[(col_index, label), ...]`` for sparse x-axis ticks.
      color_zero / color_low / color_high: RGB tuples for the colormap.
    """

    DEFAULT_CSS = """
    HeatmapGrid {
        layout: vertical;
        height: 1fr;
        width: 100%;
    }

    HeatmapGrid .heatmap-row {
        layout: horizontal;
        width: 100%;
        height: 1fr;
    }

    HeatmapGrid .heatmap-y-label {
        width: 5;
        height: 100%;
        color: #9999CC;
        content-align: right middle;
        padding: 0 1 0 0;
    }

    HeatmapGrid .heatmap-cell {
        width: 1fr;
        height: 100%;
    }

    HeatmapGrid .heatmap-x-axis {
        layout: horizontal;
        width: 100%;
        height: 1;
    }

    HeatmapGrid .heatmap-x-label {
        width: 1fr;
        height: 1;
        color: #9999CC;
        content-align: left middle;
    }
    """

    def __init__(
        self,
        grid: List[List[float]],
        y_labels: List[str],
        x_labels: Optional[List[tuple]] = None,
        color_zero: tuple = (30, 30, 30),
        color_low: tuple = (0, 80, 0),
        color_high: tuple = (0, 255, 100),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._grid = grid
        self._y_labels = y_labels
        self._x_labels = x_labels or []
        self._color_zero = color_zero
        self._color_low = color_low
        self._color_high = color_high

    @staticmethod
    def _interp(low: tuple, high: tuple, t: float) -> str:
        r = int(low[0] + t * (high[0] - low[0]))
        g = int(low[1] + t * (high[1] - low[1]))
        b = int(low[2] + t * (high[2] - low[2]))
        return f"#{r:02X}{g:02X}{b:02X}"

    def _cell_color(self, v: float, mx: float) -> str:
        if mx == 0 or v == 0:
            return f"#{self._color_zero[0]:02X}{self._color_zero[1]:02X}{self._color_zero[2]:02X}"
        # Sqrt scale compresses the top end so gradation is visible on skewed
        # data. Cells below 1% of the peak are rendered as near-blank (zero
        # color) so a single stray use doesn't light up like real activity.
        if v / mx < 0.01:
            return f"#{self._color_zero[0]:02X}{self._color_zero[1]:02X}{self._color_zero[2]:02X}"
        t = math.sqrt(v / mx)
        return self._interp(self._color_low, self._color_high, t)

    def compose(self) -> ComposeResult:
        if not self._grid or not self._grid[0]:
            return
        cols = len(self._grid[0])
        max_val = max((v for row in self._grid for v in row), default=0.0)

        # X-axis tick row (sparse: empty labels for cols without ticks)
        if self._x_labels:
            tick_map = {c: lbl for c, lbl in self._x_labels}
            x_cells = []
            for c in range(cols):
                lbl = tick_map.get(c, "")
                x_cells.append(Static(
                    f"[#9999CC]{lbl}[/]",
                    classes="heatmap-x-label", markup=True,
                ))
            yield Horizontal(
                # Spacer matching y-label width
                Static("", classes="heatmap-y-label"),
                *x_cells,
                classes="heatmap-x-axis",
            )

        for r, row in enumerate(self._grid):
            ylabel = self._y_labels[r] if r < len(self._y_labels) else ""
            label_widget = Static(
                f"[#9999CC]{ylabel}[/]",
                classes="heatmap-y-label", markup=True,
            )
            cells: list[Widget] = [label_widget]
            for v in row:
                color = self._cell_color(v, max_val)
                cell = Static("", classes="heatmap-cell")
                cell.styles.background = color
                cells.append(cell)
            yield Horizontal(*cells, classes="heatmap-row")


# ── Hourly activity bar ───────────────────────────────────────────────────

class HourlyBar(Horizontal):
    """24 equal-width cells, each a block glyph scaled to its hour's value.

    Empty hours render a ghost `░` so the 24-hour frame is always visible.
    """

    BLOCKS = " ▁▂▃▄▅▆▇█"
    EMPTY_GLYPH = "░"
    EMPTY_COLOR = "#3a3a4a"
    FILL_COLOR = "#FF9900"

    def __init__(self, values: List[float], **kwargs):
        super().__init__(**kwargs)
        if len(values) != 24:
            raise ValueError(f"HourlyBar expects 24 values, got {len(values)}")
        self._values = values

    def _cell_markup(self, v: float, mx: float) -> str:
        if v <= 0:
            glyph, color = self.EMPTY_GLYPH, self.EMPTY_COLOR
        else:
            idx = max(1, int(v / mx * (len(self.BLOCKS) - 1)))
            glyph, color = self.BLOCKS[idx], self.FILL_COLOR
        return f"[{color}]{glyph}[/]"

    def compose(self) -> ComposeResult:
        mx = max(self._values) if any(self._values) else 1.0
        for hour, v in enumerate(self._values):
            classes = "hourly-cell"
            if hour % 6 == 0 and hour != 0:
                classes += " hourly-cell-mark"
            yield Static(self._cell_markup(v, mx),
                         classes=classes, markup=True)

    def update_values(self, values: List[float]) -> None:
        """Update cell glyphs in place; preserves mounted children."""
        if len(values) != 24:
            return
        self._values = values
        mx = max(values) if any(values) else 1.0
        cells = list(self.query(Static))
        for cell, v in zip(cells, values):
            cell.update(self._cell_markup(v, mx))


# ── Interactive log row ───────────────────────────────────────────────────

# Column definitions for the call log.
# Each entry: (css_class, fixed_width_or_None)
# width=None → the activity column, which gets width: 1fr in CSS.
LOG_COLUMNS: List[tuple] = [
    ("log-col-marker",   2),   # ► / ▶ / ★ / space
    ("log-col-time",     9),   # HH:MM:SS + space
    ("log-col-model",   13),   # colored short name
    ("log-col-in",       6),   # right-aligned token count
    ("log-col-out",      6),   # right-aligned token count
    ("log-col-cache",   12),   # rK/wK right-aligned
    ("log-col-cost",     8),   # right-aligned cost
    ("log-col-bar",      9),   # block bar
    ("log-col-sub",      2),   # ↳ or space
    ("log-col-tools",    9),   # abbrev string
    ("log-col-project", 15),   # truncated path
    ("log-col-activity", None),  # fills remainder
]


class LogRow(Horizontal):
    """Fixed-column log row that posts a Clicked message carrying its index.

    Each column is a separate ``Static`` child with a fixed CSS width so
    Textual's layout engine — not Python string-padding — governs alignment.
    Markup inside one cell never shifts adjacent cells.

    ``update(columns)`` accepts a list of per-column markup strings (same
    order as ``LOG_COLUMNS``) and rewrites each child in place without
    remounting.
    """

    DEFAULT_CSS = """
    LogRow {
        height: 1;
        width: 100%;
    }
    LogRow .log-col-marker   { width: 2;   height: 1; }
    LogRow .log-col-time     { width: 9;   height: 1; }
    LogRow .log-col-model    { width: 13;  height: 1; }
    LogRow .log-col-in       { width: 6;   height: 1; content-align: right middle; }
    LogRow .log-col-out      { width: 6;   height: 1; content-align: right middle; }
    LogRow .log-col-cache    { width: 12;  height: 1; content-align: right middle; }
    LogRow .log-col-cost     { width: 8;   height: 1; content-align: right middle; }
    LogRow .log-col-bar      { width: 9;   height: 1; }
    LogRow .log-col-sub      { width: 2;   height: 1; content-align: center middle; }
    LogRow .log-col-tools    { width: 9;   height: 1; }
    LogRow .log-col-project  { width: 15;  height: 1; }
    LogRow .log-col-activity { width: 1fr; height: 1; overflow: hidden hidden; }
    """

    class Clicked(Message):
        def __init__(self, row_index: int):
            super().__init__()
            self.row_index = row_index

    def __init__(self, columns: List[str], row_index: int,
                 classes: str = "", **kwargs):
        # Strip any markup= kwarg — we always use markup
        kwargs.pop("markup", None)
        super().__init__(classes=classes, **kwargs)
        self._columns = columns
        self.row_index = row_index

    def compose(self) -> ComposeResult:
        for text, (css_cls, _width) in zip(self._columns, LOG_COLUMNS):
            yield Static(text, classes=css_cls, markup=True)

    def update(self, columns: List[str]) -> None:
        """Rewrite column text in place (no remount)."""
        self._columns = columns
        children = list(self.query(Static))
        for child, text in zip(children, columns):
            child.update(text)

    def on_click(self, event: Click) -> None:
        self.post_message(self.Clicked(self.row_index))


# ── Help modal ────────────────────────────────────────────────────────────

class HelpScreen(ModalScreen):
    """Keyboard shortcut reference overlay, context-aware by current view."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("?", "dismiss", "Close"),
    ]

    # Per-view shortcut blocks. Keyed by the active view name
    # ("BOARD", "TREND", "HEATMAP", "LOG", "CALLS").
    _VIEW_HELP = {
        "BOARD": (
            "BOARD",
            "  [b]1–4    [/b] Expand a panel fullscreen\n"
            "  [b]m      [/b] Switch board price / tokens\n"
            "  [b]h      [/b] Cycle hourly bar (cost / tokens / requests)\n"
            "  [b]j / k  [/b] Move call-log selection · [b]enter[/b] detail",
        ),
        "TREND": (
            "TREND (fullscreen)",
            "  [b]m      [/b] Cycle cost / tokens-io / tokens-cache / savings\n"
            "  [b]esc/0  [/b] Back to board · [b]] [ [/b] next/prev panel",
        ),
        "HEATMAP": (
            "HEATMAP (fullscreen)",
            "  [b]m      [/b] Cycle cost / requests / tokens\n"
            "  [b]esc/0  [/b] Back to board · [b]] [ [/b] next/prev panel",
        ),
        "LOG": (
            "CALL LOG (fullscreen)",
            "  [b]j / k  [/b] Move selection · [b]J / K[/b] by 10 · [b]g / G[/b] ends\n"
            "  [b]enter  [/b] Open entry detail\n"
            "  [b]esc/0  [/b] Back to board · [b]] [ [/b] next/prev panel",
        ),
        "CALLS": (
            "CALLS (fullscreen)",
            "  [b]esc/0  [/b] Back to board · [b]] [ [/b] next/prev panel",
        ),
    }

    _COMMON = """\
[b]Navigation[/b]
  [b]1 TREND  2 HEATMAP  3 LOG  4 CALLS[/b] — expand fullscreen
  [b]0 / esc  [/b] Return to board
  [b]] / [    [/b] Next / prev fullscreen panel
  [b]tab      [/b] Move focus (accessibility)

[b]General[/b]
  [b]r        [/b] Pause / resume auto-refresh
  [b]?        [/b] This help
  [b]q / ^C   [/b] Quit

[dim]esc / q / ? to close[/dim]\
"""

    def __init__(self, current_view: str = "BOARD", **kwargs):
        super().__init__(**kwargs)
        self._current_view = current_view

    def compose(self) -> ComposeResult:
        name, block = self._VIEW_HELP.get(
            self._current_view, self._VIEW_HELP["BOARD"]
        )
        text = (
            f"[b #FF9900]Current view: {name}[/]\n{block}\n\n{self._COMMON}"
        )
        yield Vertical(
            Static(text, id="help-body"),
            id="help-box",
        )



# ── Detail modal ──────────────────────────────────────────────────────────

class EntryDetailScreen(ModalScreen):
    """Modal showing full details of a single ledger entry."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
    ]

    def __init__(self, dt, entry_id: str, entry: Dict):
        super().__init__()
        self._dt = dt
        self._entry_id = entry_id
        self._entry = entry

    def compose(self) -> ComposeResult:
        e = self._entry
        preview = e.get("promptPreview") or "—"
        lines = [
            f"[b]{self._entry_id}[/b]",
            "",
            f"Time:     {self._dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Source:   {e.get('source', '?')}",
            f"Model:    {e.get('model') or '—'}",
            f"Project:  {e.get('project') or '—'}",
            f"Session:  {e.get('session') or '—'}",
            f"Subagent: {'yes' if e.get('isSubagent') else 'no'}",
            f"Stop:     {e.get('stopReason') or '—'}",
            "",
            f"Tokens:   in={e.get(FIELD_TOKENS_IN) or 0:,}  "
            f"out={e.get(FIELD_TOKENS_OUT) or 0:,}  "
            f"cache_w={e.get(FIELD_CACHE_WRITES) or 0:,}  "
            f"cache_r={e.get(FIELD_CACHE_READS) or 0:,}",
            f"Cost:     {format_cost(e.get(FIELD_COST))}  "
            f"(saved {format_cost(e.get(FIELD_CACHE_SAVINGS))})",
            "",
            "[b]Prompt preview:[/b]",
            preview,
            "",
            "[dim]esc / q / enter to close[/dim]",
        ]
        yield Vertical(
            Static("\n".join(lines), id="entry-detail-body"),
            id="entry-detail-box",
        )


# ── Bindable widgets ──────────────────────────────────────────────────────
#
# These subscribe to a `LiveMetrics` carrier and rewrite their content when
# the current `MetricsSnapshot` changes. They eliminate the OpsRefs registry
# and the `_apply_stat_labels` / `_update_ops_in_place` pair by making every
# live value a self-updating widget.

class LiveLabel(Label):
    """Label bound to a `MetricsSnapshot` field via a selector.

    Parameters:
      live: The `LiveMetrics` carrier to subscribe to.
      selector: `(snapshot) -> str`. Returns the full rendered markup.
      placeholder: Shown before the first snapshot arrives.
    """

    def __init__(self, live, selector: Callable, placeholder: str = "",
                 markup: bool = True, **kwargs):
        super().__init__(placeholder, markup=markup, **kwargs)
        self._live = live
        self._selector = selector

    def on_mount(self) -> None:
        self.watch(self._live, "snapshot", self._on_snapshot, init=True)

    def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        try:
            text = self._selector(snap)
        except Exception:
            return
        self.update(text)


class LiveStatic(Static):
    """Static bound to a `MetricsSnapshot` field. Same API as `LiveLabel`."""

    def __init__(self, live, selector: Callable, placeholder: str = "",
                 markup: bool = True, **kwargs):
        super().__init__(placeholder, markup=markup, **kwargs)
        self._live = live
        self._selector = selector

    def on_mount(self) -> None:
        self.watch(self._live, "snapshot", self._on_snapshot, init=True)

    def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        try:
            text = self._selector(snap)
        except Exception:
            return
        self.update(text)


class LiveBorderSubtitle(Static):
    """Zero-height helper: updates its parent panel's border_subtitle.

    Trick: watchers run on any Widget. We use a 0-content Static mounted
    inside the panel that owns the border. Avoids restructuring panel
    builders just to bind border text.
    """

    DEFAULT_CSS = "LiveBorderSubtitle { display: none; }"

    def __init__(self, live, selector: Callable, parent_widget: Widget,
                 **kwargs):
        super().__init__("", **kwargs)
        self._live = live
        self._selector = selector
        self._parent_widget = parent_widget

    def on_mount(self) -> None:
        self.watch(self._live, "snapshot", self._on_snapshot, init=True)

    def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        try:
            text = self._selector(snap)
        except Exception:
            return
        self._parent_widget.border_subtitle = text


class LiveHourlyBar(HourlyBar):
    """HourlyBar that re-reads its 24 values from the snapshot."""

    def __init__(self, live, selector: Callable, **kwargs):
        super().__init__([0.0] * 24, **kwargs)
        self._live = live
        self._selector = selector

    def on_mount(self) -> None:
        self.watch(self._live, "snapshot", self._on_snapshot, init=True)

    def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        try:
            values = self._selector(snap)
        except Exception:
            return
        self.update_values(values)


class LiveSparkline(Sparkline):
    """Sparkline bound to a list-valued snapshot selector."""

    def __init__(self, live, selector: Callable, summary_function=max,
                 **kwargs):
        super().__init__([0.0], summary_function=summary_function, **kwargs)
        self._live = live
        self._selector = selector

    def on_mount(self) -> None:
        self.watch(self._live, "snapshot", self._on_snapshot, init=True)

    def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        try:
            values = self._selector(snap)
        except Exception:
            return
        if values and any(v > 0 for v in values):
            self.data = values
