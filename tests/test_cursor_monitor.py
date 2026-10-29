"""Mouse-relative monitor intent with fake displays and caller-owned fake windows."""

import ctypes
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from voicebar.executor import native
from voicebar.executor.common import ExecutionError
from voicebar.executor.native import Monitor, NativeWindows, Window
from voicebar.executor.windows import monitor_reference_for, window

LEFT = Monitor(1, (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, "left", 96)
RIGHT = Monitor(2, (1920, 0, 3840, 1080), (1920, 0, 3840, 1040), False, "right", 96)
ABOVE = Monitor(3, (0, -1080, 1920, 0), (0, -1080, 1920, -40), False, "above", 96)
MOVE = {"action": "window", "op": "move_monitor", "target": "active", "monitor": "next"}


class FakeNative:
    def __init__(self, *, source=1, cursor=1, displays=(LEFT, RIGHT), show=1):
        self.displays = list(displays)
        self.source, self.cursor, self.show_state = source, cursor, show
        self.rectangle = (100, 100, 500, 400) if source == 1 else (2020, 100, 2420, 400)
        self.mutations = []

    def monitors(self):
        return self.displays

    def monitor(self, hwnd):
        return self.source

    def cursor_monitor(self):
        return self.cursor

    def placement(self, hwnd):
        return {"show": self.show_state, "rect": self.rectangle}

    def rect(self, hwnd):
        return self.rectangle

    def position(self, hwnd, rectangle):
        self.mutations.append(("position", rectangle))
        self.rectangle = rectangle
        self.source = 2 if rectangle[0] >= 1920 else 1

    def show(self, hwnd, command):
        self.mutations.append(("show", command))
        self.show_state = 1 if command == native.SW_RESTORE else command


def context(backend, reference="cursor"):
    target = Window(10, 20, 30, "owned fake window", "Fake", "C:\\fake.exe", ())
    begins = []

    def wait(predicate, description):
        assert predicate(), description

    return SimpleNamespace(
        native=backend, monitor_reference=reference, target=lambda _: target,
        guard=lambda window: None, begin=lambda: begins.append(True),
        wait=wait, timeout=.01, begins=begins,
    )


@pytest.mark.parametrize("text", [
    "computer move it to the other screen",
    "Computer, put Notepad on another monitor",
    "send this onto my other display",
])
def test_other_screen_uses_cursor_reference(text):
    assert monitor_reference_for(text, [MOVE]) == "cursor"


@pytest.mark.parametrize("text,direction", [
    ("move it to the next screen", "next"),
    ("move it to the second screen", "next"),
    ("move it to the previous screen", "prev"),
    ("move it to the left screen", "left"),
])
def test_explicit_directions_preserve_window_relative_semantics(text, direction):
    assert monitor_reference_for(text, [{**MOVE, "monitor": direction}]) == "window"


def test_multiple_moves_with_other_screen_are_rejected_before_execution():
    with pytest.raises(ExecutionError, match="ambiguous"):
        monitor_reference_for("open Notepad then move it to the other screen then to the next monitor",
                              [{"action": "open_app", "app": "Notepad"}, MOVE, MOVE])


def test_type_and_search_payloads_are_not_monitor_intent():
    for action in [
        {"action": "type_text", "text": "on the other screen", "submit": False},
        {"action": "web_search", "engine": "google", "query": "on the other screen"},
    ]:
        assert monitor_reference_for("move it to next screen then write on the other screen",
                                      [MOVE, action]) == "window"
    assert monitor_reference_for("move it to the other screen then write to the other screen", [
        MOVE, {"action": "type_text", "text": "to the other screen", "submit": False},
    ]) == "cursor"


@pytest.mark.parametrize("source,cursor,destination", [(1, 1, 2), (2, 2, 1)])
def test_other_screen_moves_away_from_mouse(source, cursor, destination):
    backend = FakeNative(source=source, cursor=cursor)
    ctx = context(backend)
    detail = window(ctx, MOVE)
    assert backend.source == destination
    assert backend.mutations and "confirmed" in detail
    assert len(ctx.begins) == len(backend.mutations)


@pytest.mark.parametrize("source,cursor", [(1, 2), (2, 1)])
def test_window_already_on_display_without_mouse_is_a_noop(source, cursor):
    backend = FakeNative(source=source, cursor=cursor)
    ctx = context(backend)
    assert "already" in window(ctx, MOVE)
    assert backend.source == source
    assert backend.mutations == [] and ctx.begins == []


@pytest.mark.parametrize("displays,cursor", [
    ((LEFT,), 1), ((LEFT, RIGHT, ABOVE), 1), ((LEFT, LEFT), 1), ((LEFT, RIGHT), 99),
])
def test_ambiguous_missing_or_duplicate_displays_never_mutate(displays, cursor):
    backend = FakeNative(displays=displays, cursor=cursor, show=native.SW_SHOWMAXIMIZED)
    ctx = context(backend)
    with pytest.raises(ExecutionError):
        window(ctx, MOVE)
    assert backend.mutations == [] and ctx.begins == []


def test_explicit_next_still_cycles_from_window_even_with_mouse_elsewhere():
    backend = FakeNative(source=1, cursor=2)
    window(context(backend, reference="window"), MOVE)
    assert backend.source == 2


def test_cursor_reference_does_not_reinterpret_other_monitor_directions():
    backend = FakeNative(source=1, cursor=1)
    window(context(backend), {**MOVE, "monitor": "prev"})
    assert backend.source == 2


def test_cursor_query_error_precedes_window_restoration_or_movement():
    backend = FakeNative(show=native.SW_SHOWMAXIMIZED)

    def error():
        raise RuntimeError("cursor query failed")

    backend.cursor_monitor = error
    ctx = context(backend)
    with pytest.raises(RuntimeError, match="cursor query failed"):
        window(ctx, MOVE)
    assert backend.mutations == [] and ctx.begins == []


def test_new_win32_bindings_use_documented_pointer_and_point_by_value(monkeypatch):
    bindings = {}

    def bind(dll, name, result, *arguments):
        bindings[name] = result, arguments

    monkeypatch.setattr(native, "_bind", bind)
    fake = SimpleNamespace(**dict.fromkeys(
        ("user32", "kernel32", "advapi32", "dwmapi", "shcore", "ole32", "shell32", "shlwapi"),
        object()))
    NativeWindows._bind_functions(fake)
    assert bindings["GetCursorPos"] == (native.BOOL, (ctypes.POINTER(native.POINT),))
    assert bindings["MonitorFromPoint"] == (native.HANDLE, (native.POINT, native.DWORD))


def cursor_backend(*, readable=True, monitor=2):
    backend = object.__new__(NativeWindows)
    calls = []

    @contextmanager
    def dpi():
        calls.append("dpi enter")
        try:
            yield
        finally:
            calls.append("dpi exit")

    def get_cursor(pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(native.POINT)).contents
        point.x, point.y = -100, 250
        calls.append("cursor")
        return readable

    def from_point(point, flags):
        calls.append((point.x, point.y, flags))
        return monitor

    backend._dpi = dpi
    backend._error = lambda operation: RuntimeError(f"{operation} failed")
    backend.user32 = SimpleNamespace(GetCursorPos=get_cursor, MonitorFromPoint=from_point)
    return backend, calls


def test_cursor_query_keeps_negative_virtual_coordinates_and_uses_no_guess_flag():
    backend, calls = cursor_backend()
    assert backend.cursor_monitor() == 2
    assert calls == ["dpi enter", "cursor", (-100, 250, 0), "dpi exit"]


def test_failed_cursor_read_restores_dpi_and_does_not_query_a_monitor():
    backend, calls = cursor_backend(readable=False)
    with pytest.raises(RuntimeError, match="GetCursorPos"):
        backend.cursor_monitor()
    assert calls == ["dpi enter", "cursor", "dpi exit"]


def test_cursor_outside_displays_is_not_assigned_to_nearest_monitor():
    backend, calls = cursor_backend(monitor=0)
    with pytest.raises(RuntimeError, match="not on an identified display"):
        backend.cursor_monitor()
    assert calls[-1] == "dpi exit"
