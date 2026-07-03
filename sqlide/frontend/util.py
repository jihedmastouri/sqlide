"""Small helpers shared across the app."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib


def run_async(
    work: Callable[[], Any],
    on_success: Callable[[Any], None],
    on_error: Callable[[Exception], None],
) -> None:
    """Run `work()` on a daemon thread and deliver the result (or the
    exception) back on the GTK main loop.

    Every connector call made from the UI must go through this so the
    main loop never blocks on the database.
    """

    def dispatch(callback: Callable, value: Any) -> bool:
        callback(value)
        return GLib.SOURCE_REMOVE

    def runner() -> None:
        try:
            result = work()
        except Exception as exc:
            GLib.idle_add(dispatch, on_error, exc)
        else:
            GLib.idle_add(dispatch, on_success, result)

    threading.Thread(target=runner, daemon=True).start()
