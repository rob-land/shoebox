"""Run blocking work off the GTK main loop and post results back via GLib.idle_add."""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from gi.repository import GLib


def run_async(
    func: Callable[..., Any],
    *args: Any,
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> threading.Thread:
    """Run *func(\\*args)* in a background thread.

    Result is delivered to *on_done* (or *on_error*) on the GLib main loop.
    """

    def worker() -> None:
        try:
            result = func(*args)
        except BaseException as e:  # noqa: BLE001 — we want to forward anything
            if on_error is not None:
                GLib.idle_add(_safely, on_error, e)
            return
        if on_done is not None:
            GLib.idle_add(_safely, on_done, result)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def _safely(cb: Callable[[Any], None], arg: Any) -> bool:
    try:
        cb(arg)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    return False
