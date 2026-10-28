"""Terminal-specific compatibility for the dashboard's default driver."""

import os
from functools import lru_cache
from typing import TYPE_CHECKING, Type

if TYPE_CHECKING:
    from textual.driver import Driver


@lru_cache(maxsize=1)
def _apple_terminal_driver_class() -> Type["Driver"]:
    from textual.drivers.linux_driver import LinuxDriver

    class AppleTerminalDriver(LinuxDriver):
        def _query_in_band_window_resize(self) -> None:
            # Apple Terminal renders the unsupported DECRQM query's final
            # "p" literally. LinuxDriver's SIGWINCH handling still resizes
            # the dashboard without this optional protocol.
            pass

    return AppleTerminalDriver


def compatible_driver_class(driver_class: Type["Driver"]) -> Type["Driver"]:
    """Keep Textual's driver choice, with one Apple Terminal compatibility fix."""
    if os.name != "posix" or os.environ.get("TERM_PROGRAM") != "Apple_Terminal":
        return driver_class

    # Import the POSIX driver only when needed. Exact matching preserves
    # headless, inline, web, and explicitly customized driver choices.
    from textual.drivers.linux_driver import LinuxDriver

    if driver_class is LinuxDriver:
        return _apple_terminal_driver_class()
    return driver_class


def compatible_app_class(app_class):
    """Apply startup compatibility without changing the dashboard renderer."""
    if os.name != "posix" or os.environ.get("TERM_PROGRAM") != "Apple_Terminal":
        return app_class

    class AppleTerminalApp(app_class):
        def get_driver_class(self):
            return compatible_driver_class(super().get_driver_class())

    return AppleTerminalApp
