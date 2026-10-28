"""Output-level checks for Apple Terminal startup compatibility."""

from types import SimpleNamespace

import pytest
from textual.drivers.headless_driver import HeadlessDriver
from textual.drivers.linux_driver import LinuxDriver
from textual.drivers.linux_inline_driver import LinuxInlineDriver

from lcost import terminal


class CapturedWriter:
    def __init__(self):
        self.output = []

    def write(self, data):
        self.output.append(data)


def resize_query_output(driver_class):
    # Exercising the real query and write methods does not require changing
    # this test runner's terminal settings or registering signal handlers.
    driver = object.__new__(driver_class)
    writer = CapturedWriter()
    driver._writer_thread = writer
    driver._query_in_band_window_resize()
    return "".join(writer.output)


def test_apple_terminal_omits_unsupported_resize_query(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    driver_class = terminal.compatible_driver_class(LinuxDriver)

    assert issubclass(driver_class, LinuxDriver)
    assert resize_query_output(driver_class) == ""
    assert driver_class.start_application_mode is LinuxDriver.start_application_mode
    assert driver_class._get_terminal_size is LinuxDriver._get_terminal_size


@pytest.mark.parametrize("term_program", ["iTerm.app", "vscode", "", None])
def test_other_terminals_keep_normal_capability_queries(monkeypatch, term_program):
    if term_program is None:
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
    else:
        monkeypatch.setenv("TERM_PROGRAM", term_program)

    driver_class = terminal.compatible_driver_class(LinuxDriver)

    assert driver_class is LinuxDriver
    assert resize_query_output(driver_class) == "\x1b[?2048$p"


def test_apple_terminal_preserves_headless_inline_and_custom_drivers(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    class CustomDriver(LinuxDriver):
        pass

    assert terminal.compatible_driver_class(HeadlessDriver) is HeadlessDriver
    assert terminal.compatible_driver_class(LinuxInlineDriver) is LinuxInlineDriver
    assert terminal.compatible_driver_class(CustomDriver) is CustomDriver


def test_non_posix_driver_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        terminal, "os",
        SimpleNamespace(name="nt", environ={"TERM_PROGRAM": "Apple_Terminal"}),
    )

    assert terminal.compatible_driver_class(LinuxDriver) is LinuxDriver


def test_apple_terminal_app_keeps_original_display_methods(monkeypatch):
    from lcost.tui import CostTrackerApp

    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    app_class = terminal.compatible_app_class(CostTrackerApp)
    app = app_class(demo=True)

    assert resize_query_output(app.get_driver_class()) == ""
    assert app_class.compose is CostTrackerApp.compose
    assert app_class.on_mount is CostTrackerApp.on_mount
    assert app_class._render_board is CostTrackerApp._render_board


@pytest.mark.parametrize("term_program", ["ghostty", "iTerm.app", "vscode", None])
def test_other_terminals_use_original_dashboard_class(monkeypatch, term_program):
    from lcost.tui import CostTrackerApp

    if term_program is None:
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
    else:
        monkeypatch.setenv("TERM_PROGRAM", term_program)
    assert terminal.compatible_app_class(CostTrackerApp) is CostTrackerApp
