"""Window actions with state confirmation and monitor scaling."""

from __future__ import annotations

import re

from .common import ExecutionError
from .native import (
    SW_RESTORE,
    SW_SHOWMAXIMIZED,
    SW_SHOWMINIMIZED,
    SW_SHOWNORMAL,
    NativeWaitTimeout,
)


def _mutate(ctx, target, operation, *args):
    ctx.guard(target)
    ctx.begin()
    operation(target.hwnd, *args)


def _show(ctx, target, command, expected):
    _mutate(ctx, target, ctx.native.show, command)
    ctx.wait(lambda: ctx.native.placement(target.hwnd)["show"] == expected,
             f"window show state {expected}")


def _same_rect(actual, expected, tolerance=2):
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True))


def _normal(ctx, target):
    state = ctx.native.placement(target.hwnd)["show"]
    if state in (SW_SHOWMAXIMIZED, SW_SHOWMINIMIZED):
        _mutate(ctx, target, ctx.native.show, SW_RESTORE)
        ctx.wait(lambda: ctx.native.placement(target.hwnd)["show"] != SW_SHOWMINIMIZED,
                 "window restoration")
        # A minimized window can restore into its previous maximized state.
        if ctx.native.placement(target.hwnd)["show"] == SW_SHOWMAXIMIZED:
            _show(ctx, target, SW_RESTORE, SW_SHOWNORMAL)


def _destination(monitors, current, direction):
    ordered = sorted(monitors, key=lambda mon: (mon.rect[0], mon.rect[1], mon.device))
    if not ordered:
        raise ExecutionError("Windows did not report a connected monitor.")
    if direction == "primary":
        primary = next((monitor for monitor in ordered if monitor.primary), None)
        if primary is None:
            raise ExecutionError("Windows did not identify a primary monitor.")
        return primary
    if direction == "left":
        return ordered[0]
    if direction == "right":
        return ordered[-1]
    if direction not in ("next", "prev"):
        raise ExecutionError(f"Unknown monitor direction: {direction}.")
    index = next((index for index, monitor in enumerate(ordered) if monitor.handle == current), None)
    if index is None:
        raise ExecutionError("The window's monitor is no longer connected.")
    return ordered[(index + (1 if direction == "next" else -1)) % len(ordered)]


_OTHER_MONITOR = re.compile(
    r"\b(?:to|on|onto)\s+(?:(?:the|my|an)\s+)?(?:other|another)\s+"
    r"(?:screen|monitor|display)\b", re.IGNORECASE)


def monitor_reference_for(text: str, plan: list[dict]) -> str:
    """Associate 'other screen' with the mouse without changing action enums.

    The closed plan stores both 'other' and explicit 'next' as monitor=next.
    Original speech supplies this execution-only distinction. A multi-move
    utterance cannot be associated safely, so reject it before any action.
    """
    # A mention inside text to type or a query is data, not monitor intent.
    command_text = text
    for action in plan:
        field = {"type_text": "text", "web_search": "query"}.get(action.get("action"))
        if field:
            payload = action.get(field)
            if isinstance(payload, str) and payload:
                start = command_text.rfind(payload)
                if start >= 0:
                    command_text = (command_text[:start] + " " * len(payload)
                                    + command_text[start + len(payload):])
    if not _OTHER_MONITOR.search(command_text):
        return "window"
    moves = [action for action in plan
             if action.get("action") == "window" and action.get("op") == "move_monitor"]
    if len(moves) > 1:
        raise ExecutionError("Multiple monitor moves with 'other screen' are ambiguous; say one move at a time.")
    if len(moves) == 1 and moves[0].get("monitor") == "next":
        return "cursor"
    return "window"


def _other_destination(monitors, cursor):
    if len(monitors) != 2 or len({monitor.handle for monitor in monitors}) != 2:
        raise ExecutionError("'Other screen' needs exactly two connected monitors; choose a named direction.")
    if not any(monitor.handle == cursor for monitor in monitors):
        raise ExecutionError("The mouse's display is no longer connected.")
    return next(monitor for monitor in monitors if monitor.handle != cursor)


def _translated_rect(rect, source, destination):
    """Keep logical size, relative position, and a fully visible destination rect."""
    left, top, right, bottom = rect
    sx, sy, sr, sb = source.work
    dx, dy, dr, db = destination.work
    if min(sr - sx, sb - sy, dr - dx, db - dy, source.dpi, destination.dpi) <= 0:
        raise ExecutionError("A monitor has invalid work-area or scaling information.")
    old_width, old_height = right - left, bottom - top
    ratio = destination.dpi / source.dpi
    width = min(max(1, round(old_width * ratio)), dr - dx)
    height = min(max(1, round(old_height * ratio)), db - dy)
    relative_x = max(0.0, min(1.0, (left - sx) / max(1, sr - sx - old_width)))
    relative_y = max(0.0, min(1.0, (top - sy) / max(1, sb - sy - old_height)))
    new_left = dx + round(relative_x * (dr - dx - width))
    new_top = dy + round(relative_y * (db - dy - height))
    return new_left, new_top, new_left + width, new_top + height


def window(ctx, action: dict) -> str:
    target = ctx.target(action.get("target", "active"))
    ctx.guard(target)
    operation = action["op"]
    if operation in ("maximize", "minimize", "restore"):
        command, expected = {
            "maximize": (SW_SHOWMAXIMIZED, SW_SHOWMAXIMIZED),
            "minimize": (SW_SHOWMINIMIZED, SW_SHOWMINIMIZED),
            "restore": (SW_RESTORE, SW_SHOWNORMAL),
        }[operation]
        if operation == "restore":
            _normal(ctx, target)
            if ctx.native.placement(target.hwnd)["show"] != SW_SHOWNORMAL:
                _show(ctx, target, command, expected)
        else:
            _show(ctx, target, command, expected)
        return f"{operation.capitalize()} confirmed for {target.title}."
    if operation == "close":
        before = {value.hwnd for value in ctx.native.windows()}
        _mutate(ctx, target, ctx.native.close)

        def closed_or_prompt():
            if not ctx.native.is_window(target.hwnd, target.pid):
                return "closed"
            if any(value.pid == target.pid and value.hwnd not in before
                   for value in ctx.native.windows()):
                return "prompt"
            return None

        try:
            state = ctx.native.wait(closed_or_prompt, min(ctx.timeout, 2.0), "window close or prompt")
        except NativeWaitTimeout:
            state = "pending"
        if state == "closed":
            return f"Closed {target.title}; window disappearance confirmed."
        raise ExecutionError("WM_CLOSE was posted; the window remains open. Any unsaved-work prompt is left for you.")
    if operation in ("snap_left", "snap_right"):
        monitors = ctx.native.monitors()
        handle = ctx.native.monitor(target.hwnd)
        monitor = next((value for value in monitors if value.handle == handle), None)
        if monitor is None:
            raise ExecutionError("The window's monitor could not be identified.")
        _normal(ctx, target)
        left, top, right, bottom = monitor.work
        middle = left + (right - left) // 2
        rectangle = ((left, top, middle, bottom) if operation == "snap_left"
                     else (middle, top, right, bottom))
        _mutate(ctx, target, ctx.native.position, rectangle)
        ctx.wait(lambda: _same_rect(ctx.native.rect(target.hwnd), rectangle),
                 "window to occupy the requested monitor half")
        return f"{operation.replace('_', ' ').capitalize()} confirmed for {target.title}."
    if operation == "move_monitor":
        monitors = ctx.native.monitors()
        handle = ctx.native.monitor(target.hwnd)
        source = next((value for value in monitors if value.handle == handle), None)
        if source is None:
            raise ExecutionError("The window's monitor could not be identified.")
        if getattr(ctx, "monitor_reference", "window") == "cursor" and action["monitor"] == "next":
            destination = _other_destination(monitors, ctx.native.cursor_monitor())
        else:
            destination = _destination(monitors, handle, action["monitor"])
        if destination.handle == source.handle:
            return ("Only one monitor is connected; window stayed on that monitor."
                    if len(monitors) == 1 else "The window is already on the requested monitor.")
        previous_show = ctx.native.placement(target.hwnd)["show"]
        maximized = previous_show == SW_SHOWMAXIMIZED
        if previous_show == SW_SHOWMINIMIZED:
            _mutate(ctx, target, ctx.native.show, SW_RESTORE)
            ctx.wait(lambda: ctx.native.placement(target.hwnd)["show"] != SW_SHOWMINIMIZED,
                     "minimized window restoration")
            maximized = ctx.native.placement(target.hwnd)["show"] == SW_SHOWMAXIMIZED
        _normal(ctx, target)
        rectangle = _translated_rect(ctx.native.rect(target.hwnd), source, destination)
        _mutate(ctx, target, ctx.native.position, rectangle)
        ctx.wait(lambda: ctx.native.monitor(target.hwnd) == destination.handle,
                 "window to reach the destination monitor")
        # DPI-aware apps can resize in response to WM_DPICHANGED. Set the desired
        # logical size once more after crossing, then verify the actual geometry.
        _mutate(ctx, target, ctx.native.position, rectangle)
        ctx.wait(lambda: _same_rect(ctx.native.rect(target.hwnd), rectangle),
                 "window placement at destination scaling")
        if maximized:
            _show(ctx, target, SW_SHOWMAXIMIZED, SW_SHOWMAXIMIZED)
        if previous_show == SW_SHOWMINIMIZED:
            _show(ctx, target, SW_SHOWMINIMIZED, SW_SHOWMINIMIZED)
        ctx.wait(lambda: ctx.native.monitor(target.hwnd) == destination.handle,
                 "final window monitor")
        return f"Moved {target.title} to {destination.device}; monitor and show state confirmed."
    raise ExecutionError(f"Window operation is not supported: {operation}.")
