"""ABI and deterministic window-state checks without opening real windows."""

import ctypes
import threading
from types import SimpleNamespace

import pytest

from voicebar.executor import native
from voicebar.executor.common import ExecutionError
from voicebar.executor.native import Monitor, NativeWindows, Window
from voicebar.executor.windows import _destination, _translated_rect, window


def test_windows_abi_uses_fixed_width_fields_and_mac_field_is_absent():
    assert ctypes.sizeof(native.DWORD) == 4
    assert ctypes.sizeof(native.LONG) == 4
    assert ctypes.sizeof(native.RECT) == 16
    assert ctypes.sizeof(native.WINDOWPLACEMENT) == 44
    assert native.WINDOWPLACEMENT.rcNormalPosition.offset == 28
    assert not hasattr(native.WINDOWPLACEMENT, "rcDevice")
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(native.PROPVARIANT) == 24
        assert ctypes.sizeof(native.GUITHREADINFO) == 72


def test_non_windows_constructor_fails_before_loading_a_dll(monkeypatch):
    monkeypatch.setattr(native.os, "name", "posix")
    monkeypatch.setattr(native.C, "WinDLL", lambda *a, **k: pytest.fail("DLL was loaded"), raising=False)
    with pytest.raises(RuntimeError, match="native Windows"):
        NativeWindows()


def test_appsfolder_launch_uses_fixed_explorer_and_literal_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(native.subprocess, "Popen", lambda argv, **options: calls.append((argv, options)))
    backend = object.__new__(NativeWindows)
    backend.launch_app("Registered.App!Literal;Value")
    argv, options = calls[0]
    assert argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold() == "explorer.exe"
    assert argv[1] == "shell:AppsFolder\\Registered.App!Literal;Value"
    assert options["shell"] is False
    with pytest.raises(RuntimeError, match="invalid AppID"):
        backend.launch_app("bad\0id")
    assert len(calls) == 1


def test_wait_checks_predicate_before_deadline_and_reports_what_failed():
    value = object()
    assert NativeWindows.wait(lambda: value, 0, "already ready") is value
    with pytest.raises(native.NativeWaitTimeout, match="target window"):
        NativeWindows.wait(lambda: False, 0, "target window")


@pytest.mark.parametrize("status,uninitializations", [(0, 1), (1, 1), (-2147417850, 0)])
def test_com_apartment_lives_across_operations_and_balances_only_owned_init(status, uninitializations):
    calls = []
    backend = object.__new__(NativeWindows)
    backend._com_state = threading.local()
    backend._com_exit_registered = True  # Do not register fake backend at process exit.
    backend.ole32 = SimpleNamespace(
        CoInitializeEx=lambda *args: calls.append("initialize") or status,
        CoUninitialize=lambda: calls.append("uninitialize"),
    )
    with backend._com(), backend._com():
        assert calls == ["initialize"]
    with backend._com():
        assert calls == ["initialize"]
    backend.shutdown()
    backend.shutdown()
    assert calls == ["initialize"] + ["uninitialize"] * uninitializations


def test_process_identity_queries_sized_aumid_and_closes_handle():
    calls = []

    class Kernel:
        def OpenProcess(self, access, inherit, pid):
            assert (access, inherit, pid) == (0x1000, False, 73)
            return 123

        def QueryFullProcessImageNameW(self, handle, flags, buffer, size):
            buffer.value = "C:\\Windows\\System32\\ApplicationFrameHost.exe"
            return True

        def GetApplicationUserModelId(self, handle, count, buffer):
            if buffer is None:
                ctypes.cast(count, ctypes.POINTER(native.UINT)).contents.value = 12
                return 122
            buffer.value = "Package!App"
            return 0

        def CloseHandle(self, handle):
            calls.append(handle)

    backend = object.__new__(NativeWindows)
    backend.kernel32 = Kernel()
    assert backend._process_identity(73) == (
        "C:\\Windows\\System32\\ApplicationFrameHost.exe", "Package!App"
    )
    assert calls == [123]


def test_hosted_package_identity_is_attached_to_its_top_level_frame():
    backend = object.__new__(NativeWindows)

    class User:
        def EnumWindows(self, callback, data):
            for hwnd in (1, 2, 3, 4):
                assert callback(hwnd, data)
            return True

        def IsWindowVisible(self, hwnd):
            return True

        def GetWindowLongPtrW(self, hwnd, index):
            return 0x80 if hwnd == 2 else 0

    class Dwm:
        def DwmGetWindowAttribute(self, hwnd, attribute, output, size):
            assert attribute == 14
            ctypes.cast(output, ctypes.POINTER(native.DWORD)).contents.value = int(hwnd == 3)
            return 0

    backend.user32 = User()
    backend.dwmapi = Dwm()
    backend._pid_tid = lambda hwnd: (40, 50)
    backend._title = lambda hwnd: "" if hwnd == 4 else "Window"
    backend._class = lambda hwnd: "ApplicationFrameWindow"
    backend._window_aumid = lambda hwnd: ""
    backend.children = lambda hwnd: [{"pid": 60}]
    backend._process_identity = lambda pid: (
        ("C:\\Windows\\System32\\ApplicationFrameHost.exe", "") if pid == 40
        else ("C:\\Package\\CalculatorApp.exe", "Microsoft.WindowsCalculator_package!App")
    )
    values = backend.windows()
    assert len(values) == 1
    assert values[0].hwnd == 1
    assert values[0].pid == 40
    assert values[0].aumids == ("Microsoft.WindowsCalculator_package!App",)


LEFT = Monitor(1, (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, "left", 96)
RIGHT = Monitor(2, (1920, 0, 4480, 1440), (1920, 0, 4480, 1400), False, "right", 144)


def test_monitor_selection_sorts_by_position_and_cycles():
    assert _destination([RIGHT, LEFT], 2, "next") == LEFT
    assert _destination([RIGHT, LEFT], 1, "prev") == RIGHT
    assert _destination([RIGHT, LEFT], 2, "left") == LEFT
    assert _destination([RIGHT, LEFT], 1, "right") == RIGHT
    assert _destination([RIGHT, LEFT], 2, "primary") == LEFT
    with pytest.raises(ExecutionError, match="no longer connected"):
        _destination([LEFT], 9, "next")


def test_move_scales_logical_size_and_clamps_negative_origin():
    destination = Monitor(3, (-1280, -200, 0, 824), (-1280, -200, 0, 784), False, "negative", 192)
    rectangle = _translated_rect((100, 100, 500, 400), LEFT, destination)
    assert rectangle[2] - rectangle[0] == 800
    assert rectangle[3] - rectangle[1] == 600
    assert -1280 <= rectangle[0] < rectangle[2] <= 0
    assert -200 <= rectangle[1] < rectangle[3] <= 784
    assert _translated_rect((-500, -500, 3000, 2000), LEFT, destination) == destination.work


class FakeNative:
    def __init__(self, *, monitors=(LEFT, RIGHT), show=1, restore_max=False):
        self.displays = list(monitors)
        self.show_state = show
        self.restore_max = restore_max
        self.rectangle = (100, 100, 500, 400)
        self.monitor_handle = 1
        self.mutations = []

    def placement(self, hwnd):
        return {"show": self.show_state, "rect": self.rectangle}

    def monitors(self):
        return self.displays

    def monitor(self, hwnd):
        return self.monitor_handle

    def rect(self, hwnd):
        return self.rectangle

    def show(self, hwnd, command):
        self.mutations.append(("show", command))
        if command == 9:
            self.show_state = 3 if self.show_state == 2 and self.restore_max else 1
        else:
            self.show_state = command

    def position(self, hwnd, rectangle):
        self.mutations.append(("position", rectangle))
        self.rectangle = rectangle
        self.monitor_handle = 2 if rectangle[0] >= 1920 else 1

    wait = staticmethod(NativeWindows.wait)


def context(backend, *, denied=False):
    target = Window(10, 20, 30, "owned", "Fake", "C:\\fake.exe", ())
    events = []

    def guard(window):
        events.append("guard")
        if denied:
            raise ExecutionError("not owned")

    return SimpleNamespace(
        native=backend, timeout=0.01, target=lambda _: target,
        guard=guard, begin=lambda: events.append("begin"),
        wait=lambda predicate, description: backend.wait(predicate, 0.01, description),
        events=events,
    )


def test_maximized_move_keeps_maximized_and_destination_monitor():
    backend = FakeNative(show=3)
    ctx = context(backend)
    detail = window(ctx, {"op": "move_monitor", "target": "active", "monitor": "next"})
    assert backend.monitor_handle == 2
    assert backend.show_state == 3
    assert backend.rectangle[2] - backend.rectangle[0] == 600
    assert backend.rectangle[3] - backend.rectangle[1] == 450
    assert backend.mutations[0] == ("show", 9)
    assert backend.mutations[-1] == ("show", 3)
    assert ctx.events.count("begin") == len(backend.mutations)
    assert "confirmed" in detail


def test_one_monitor_move_is_explicit_noop_without_native_mutation():
    backend = FakeNative(monitors=(LEFT,))
    detail = window(context(backend), {"op": "move_monitor", "monitor": "next"})
    assert "Only one monitor" in detail
    assert backend.mutations == []


def test_restore_a_minimized_formerly_maximized_window_reaches_normal():
    backend = FakeNative(show=2, restore_max=True)
    window(context(backend), {"op": "restore"})
    assert backend.show_state == 1
    assert backend.mutations == [("show", 9), ("show", 9)]


def test_guard_refusal_precedes_every_window_mutation():
    backend = FakeNative()
    with pytest.raises(ExecutionError, match="not owned"):
        window(context(backend, denied=True), {"op": "maximize"})
    assert backend.mutations == []


def test_snap_uses_work_area_including_taskbar_and_odd_width():
    display = Monitor(1, (0, 0, 1921, 1080), (0, 30, 1921, 1040), True, "display", 96)
    backend = FakeNative(monitors=(display,), show=3)
    window(context(backend), {"op": "snap_right"})
    assert backend.rectangle == (960, 30, 1921, 1040)
    assert backend.show_state == 1


def test_close_does_not_hide_native_query_errors_as_pending():
    backend = FakeNative()
    queries = []

    def enumerate_windows():
        queries.append(True)
        if len(queries) > 1:
            raise RuntimeError("EnumWindows failed")
        return []

    backend.windows = enumerate_windows
    backend.close = lambda hwnd: backend.mutations.append(("close", hwnd))
    backend.is_window = lambda hwnd, pid: True
    with pytest.raises(RuntimeError, match="EnumWindows failed"):
        window(context(backend), {"op": "close"})
    assert backend.mutations == [("close", 10)]


def test_close_reports_pending_only_on_state_deadline():
    backend = FakeNative()
    backend.windows = list
    backend.close = lambda hwnd: None

    def deadline(*args):
        raise native.NativeWaitTimeout("Window remains open")

    backend.wait = deadline
    with pytest.raises(ExecutionError, match="remains open"):
        window(context(backend), {"op": "close"})
