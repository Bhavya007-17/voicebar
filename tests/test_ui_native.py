"""UI native contracts; fake key events and shortcuts only in pytest's directory."""

import ctypes as C
import os
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar.executor.native import DWORD, GUID, HANDLE, HRESULT, MSG
from voicebar.ui import native


def test_single_instance_rejects_duplicate_and_releases_its_handle():
    import uuid
    first = native.SingleInstance(name="Local\\Voicebar.Test." + uuid.uuid4().hex)
    # Save the name separately for the native collision probe.
    first.close()
    name = "Local\\Voicebar.Test." + uuid.uuid4().hex
    first = native.SingleInstance(name=name)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            native.SingleInstance(name=name)
    finally:
        first.close()
    replacement = native.SingleInstance(name=name)
    replacement.close()


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakePump:
    def __init__(self, *, failure=None):
        self.messages = queue.Queue()
        self.closed = []
        self.registered = []
        self.held = threading.Event()
        self.available = threading.Event()
        self.available.set()
        self.failure = failure
        self.controllers = []
        self.user32 = SimpleNamespace(PeekMessageW=self.peek)

    def peek(self, address, *unused):
        try:
            kind, identifier = self.messages.get_nowait()
        except queue.Empty:
            return False
        message = C.cast(address, C.POINTER(MSG)).contents
        message.message, message.wParam = kind, identifier
        return True

    def controller(self, shortcut):
        pump = self

        class Controller:
            message_type = MSG
            user32 = pump.user32

            def register(self):
                if shortcut.text == pump.failure:
                    raise RuntimeError("hotkey collision")
                pump.registered.append(self.identifier)

            def held(self):
                return pump.held.is_set()

            def available(self):
                return pump.available.is_set()

            def close(self):
                pump.closed.append(self.identifier)

        controller = Controller()
        self.controllers.append(controller)
        return controller


def test_hotkeys_register_on_worker_emit_commands_release_hold_and_clean_up():
    events, pump = queue.Queue(), FakePump()
    manager = native.HotkeyManager(events, controller_factory=pump.controller)
    manager.start()
    assert manager.ready.wait(1)
    assert manager.error is None
    assert len(pump.registered) == 3
    pump.messages.put((0x312, pump.registered[0]))
    assert events.get(timeout=1) == "toggle_listening"
    pump.messages.put((0x312, pump.registered[1]))
    assert events.get(timeout=1) == "toggle_bar"
    pump.held.set()
    pump.messages.put((0x312, pump.registered[2]))
    assert events.get(timeout=1) == "hold_start"
    pump.held.clear()
    assert events.get(timeout=1) == "hold_end"
    assert manager.close(timeout=1)
    assert pump.closed == list(reversed(pump.registered))


def test_hotkey_registration_collision_unregisters_preceding_shortcuts():
    events, pump = queue.Queue(), FakePump(failure="ctrl+alt+f10")
    manager = native.HotkeyManager(events, controller_factory=pump.controller)
    manager.start()
    assert manager.stopped.wait(1)
    assert events.get(timeout=1) == "hotkey_error"
    assert "collision" in manager.error
    assert pump.closed == [0x5646, 0x5645]


def test_hotkey_close_does_not_wait_by_default():
    manager = native.HotkeyManager(queue.Queue())
    manager._worker = SimpleNamespace(join=lambda timeout: pytest.fail("UI close blocked"))
    assert manager.close() is False
    assert manager._stop.is_set()


def test_hotkeys_release_on_lock_and_ignore_locked_shortcuts():
    events, pump = queue.Queue(), FakePump()
    manager = native.HotkeyManager(events, controller_factory=pump.controller)
    manager.start()
    assert manager.ready.wait(1)
    pump.held.set()
    pump.messages.put((0x312, pump.registered[2]))
    assert events.get(timeout=1) == "hold_start"
    pump.available.clear()
    assert events.get(timeout=1) == "hold_end"
    pump.messages.put((0x312, pump.registered[0]))
    assert manager.close(timeout=1)
    assert events.empty()


def test_duplicate_configured_hotkeys_fail_before_any_native_registration():
    with pytest.raises(RuntimeError, match="must be different"):
        native.HotkeyManager(queue.Queue(), toggle_listening="ctrl+alt+f8")


def test_no_activate_styles_remove_appwindow_and_position_without_activation():
    calls = []
    original = native.WS_EX_APPWINDOW | 0x80000
    user = SimpleNamespace(
        GetWindowLongPtrW=Function(lambda hwnd, index: original),
        SetWindowLongPtrW=Function(lambda *args: calls.append(("style", args)) or original),
        SetWindowPos=Function(lambda *args: calls.append(("position", args)) or 1),
    )
    style = native.apply_no_activate(712, user32=user)
    assert style == native.WS_EX_NOACTIVATE | native.WS_EX_TOOLWINDOW | 0x80000
    assert calls[0] == ("style", (712, -20, style))
    assert calls[1] == ("position", (712, -1, 0, 0, 0, 0, 0x33))
    assert native.handle_window_message(native.WM_MOUSEACTIVATE, 0) == (True, 3, None)


@pytest.mark.parametrize("message,wparam,result", [
    (native.WM_POWERBROADCAST, 4, (True, 1, "suspend")),
    (native.WM_POWERBROADCAST, 7, (True, 1, "resume")),
    (native.WM_POWERBROADCAST, 18, (True, 1, "resume")),
    (native.WM_WTSSESSION_CHANGE, 7, (True, 0, "locked")),
    (native.WM_WTSSESSION_CHANGE, 8, (True, 0, "unlocked")),
    (native.WM_DISPLAYCHANGE, 0, (False, 0, "display_changed")),
    (native.WM_POWERBROADCAST, 10, (False, 0, None)),
])
def test_power_session_and_display_message_decoding(message, wparam, result):
    assert native.handle_window_message(message, wparam) == result


def test_session_registration_is_limited_to_this_session_and_balanced():
    calls = []
    dll = SimpleNamespace(
        WTSRegisterSessionNotification=Function(lambda *args: calls.append(("register", args)) or 1),
        WTSUnRegisterSessionNotification=Function(lambda *args: calls.append(("unregister", args)) or 1),
    )
    notifications = native.SessionNotifications(712, wtsapi32=dll)
    notifications.close()
    notifications.close()
    assert calls == [("register", (712, 0)), ("unregister", (712,))]


def test_startup_is_opt_in_targets_windowless_python_and_only_removes_its_link(tmp_path, monkeypatch):
    executable = tmp_path / "pythonw.exe"
    executable.write_bytes(b"test target, never launched")
    startup = tmp_path / "startup"
    startup.mkdir()
    unrelated = startup / "another application.lnk"
    unrelated.write_bytes(b"unrelated")
    root = tmp_path / "root with spaces $ (literal)"
    root.mkdir()
    calls = []

    def create(path, target, arguments, working_directory):
        calls.append((path, target, arguments, working_directory))
        path.write_bytes(b"fake shortcut")

    monkeypatch.setattr(native, "_create_shortcut", create)
    entry = native.StartupShortcut(root, startup_folder=startup, executable=executable)
    assert not entry.enabled
    assert calls == []
    entry.set_enabled(True)
    assert entry.enabled
    path, target, arguments, working = calls[0]
    assert path.name == "Voicebar.pending.lnk"
    assert target == executable
    assert str(root) in arguments
    assert "main(['app'], project_root=Path(sys.argv[1]))" in arguments
    assert working == root
    entry.set_enabled(False)
    assert not entry.enabled
    assert unrelated.read_bytes() == b"unrelated"
    assert not path.exists()


def test_startup_failed_creation_leaves_previous_shortcut_and_removes_partial(tmp_path, monkeypatch):
    executable = tmp_path / "pythonw.exe"
    executable.write_bytes(b"never launched")
    entry = native.StartupShortcut(tmp_path, startup_folder=tmp_path, executable=executable)
    entry.path.write_bytes(b"previous shortcut")

    def fail(path, *unused):
        path.write_bytes(b"partial")
        raise OSError("cannot save")

    monkeypatch.setattr(native, "_create_shortcut", fail)
    with pytest.raises(OSError, match="cannot save"):
        entry.set_enabled(True)
    assert entry.path.read_bytes() == b"previous shortcut"
    assert not entry.path.with_name("Voicebar.pending.lnk").exists()


@pytest.mark.skipif(os.name != "nt", reason="Native shortcut ABI requires Windows")
def test_native_shell_shortcut_roundtrip_only_in_owned_test_directory(tmp_path):
    target = Path(__import__("sys").executable).with_name("pythonw.exe")
    path = tmp_path / "Voicebar test.lnk"
    arguments = '-c "print(123)"'
    native._create_shortcut(path, target, arguments, tmp_path)
    assert path.is_file()
    with native._shell_link() as link:
        persist = HANDLE()
        iid = native._guid("0000010B-0000-0000-C000-000000000046")
        native._check_hresult(native._com_method(
            link, 0, HRESULT, C.POINTER(GUID), C.POINTER(HANDLE))(
            link, C.byref(iid), C.byref(persist)), "Query test persistence")
        try:
            native._check_hresult(native._com_method(persist, 5, HRESULT, C.c_wchar_p, DWORD)(
                persist, str(path), 0), "Load owned test shortcut")
        finally:
            native._com_method(persist, 2, DWORD)(persist)
        buffer = C.create_unicode_buffer(32768)
        native._check_hresult(native._com_method(
            link, 3, HRESULT, C.c_wchar_p, C.c_int, C.c_void_p, DWORD)(
            link, buffer, len(buffer), None, 0), "Read test target")
        assert Path(buffer.value) == target
        native._check_hresult(native._com_method(link, 10, HRESULT, C.c_wchar_p, C.c_int)(
            link, buffer, len(buffer)), "Read test arguments")
        assert buffer.value == arguments
        native._check_hresult(native._com_method(link, 8, HRESULT, C.c_wchar_p, C.c_int)(
            link, buffer, len(buffer)), "Read test working directory")
        assert Path(buffer.value) == tmp_path
