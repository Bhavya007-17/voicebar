"""Read-only Windows UI Automation inspection and pointer observation."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def inspect_foreground(root, *, output=None, output_fn=print, native=None, automation=None):
    """Write one foreground tree and display its actionable controls.

    Injected adapters remain owned by the caller. Native adapters constructed
    here are closed even when a window disappears during the query.
    """
    own_native, own_automation = native is None, automation is None
    try:
        if native is None:
            from .executor.native import NativeWindows
            native = NativeWindows()
        hwnd = native.foreground()
        if not hwnd:
            raise RuntimeError("No foreground window is available to inspect")
        if automation is None:
            from .executor.uia import Automation
            automation = Automation()
        controls = automation.snapshot(hwnd)
        captured = datetime.now(UTC)
        target = (Path(output) if output is not None else Path(root) / "logs"
                  / f"inspect-{captured:%Y%m%dT%H%M%S%fZ}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"timestamp": captured.isoformat(), "hwnd": hwnd,
                                      "controls": controls}, ensure_ascii=False, indent=2)
                          + "\n", encoding="utf-8")
        output_fn(f"UI Automation tree: {target}")
        clickable = [control for control in controls if control.get("clickable")]
        output_fn(f"{len(controls)} controls; {len(clickable)} look clickable:")
        for control in clickable:
            output_fn(json.dumps({key: control.get(key) for key in (
                "name", "role", "automation_id", "keyboard_shortcut", "enabled")},
                ensure_ascii=False))
        return target
    finally:
        try:
            if own_automation and automation is not None:
                automation.close()
        finally:
            if own_native and native is not None:
                native.shutdown()


def watch_controls(*, duration=None, output_fn=print, automation=None, stop_event=None,
                   clock=time.monotonic, poll_interval=0.1):
    """Print changed UIA pointer controls until interrupted or a duration expires.

    The interruptible interval samples pointer movement; it never delays an
    action or hides a state-verification race.
    """
    if duration is not None and (isinstance(duration, bool)
                                 or not isinstance(duration, (int, float))
                                 or not math.isfinite(duration) or duration <= 0):
        raise ValueError("inspect --duration must be finite positive seconds")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("Pointer sample interval must be finite positive seconds")
    stop = stop_event if stop_event is not None else threading.Event()
    own_automation = automation is None
    previous, count = None, 0
    try:
        if automation is None:
            from .executor.uia import Automation
            automation = Automation()
        deadline = clock() + duration if duration is not None else None
        output_fn("Watching the control under the pointer; Ctrl+C stops.")
        while not stop.is_set():
            if deadline is not None and clock() >= deadline:
                break
            control = automation.under_pointer()
            serialized = json.dumps(control, ensure_ascii=False, sort_keys=True)
            if serialized != previous:
                output_fn(serialized)
                previous, count = serialized, count + 1
            remaining = deadline - clock() if deadline is not None else poll_interval
            if remaining <= 0:
                break
            stop.wait(min(poll_interval, remaining))
    except KeyboardInterrupt:
        output_fn("Inspection watch stopped.")
    finally:
        if own_automation and automation is not None:
            automation.close()
    return count
