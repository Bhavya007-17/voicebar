"""Windows-only UI plumbing; importing it opens no windows, devices, or sockets."""

from __future__ import annotations

import ctypes as C
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from ..executor.native import (
    BOOL,
    DWORD,
    GUID,
    HANDLE,
    HRESULT,
    HWND,
    LPARAM,
    _bind,
    _guid,
)
from ..ptt import PttError, WindowsHotkey, parse_hotkey

WM_MOUSEACTIVATE = 0x21
MA_NOACTIVATE = 3
WM_DISPLAYCHANGE = 0x7E
WM_POWERBROADCAST = 0x218
WM_WTSSESSION_CHANGE = 0x2B1
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000


class SingleInstance:
    """One bar per Windows session, without a socket or an admin service."""

    def __init__(self, *, kernel=None, name=r"Local\Voicebar.Stage5"):
        self.kernel = kernel if kernel is not None else _windows_dll("kernel32")
        _bind(self.kernel, "CreateMutexW", HANDLE, HANDLE, BOOL, C.c_wchar_p)
        _bind(self.kernel, "CloseHandle", BOOL, HANDLE)
        _bind(self.kernel, "CreateEventW", HANDLE, HANDLE, BOOL, BOOL, C.c_wchar_p)
        _bind(self.kernel, "SetEvent", BOOL, HANDLE)
        _bind(self.kernel, "WaitForSingleObject", DWORD, HANDLE, DWORD)
        self.activation = self.kernel.CreateEventW(None, False, False, name + ".Activate")
        if not self.activation:
            raise C.WinError(C.get_last_error())
        C.set_last_error(0)
        self.handle = self.kernel.CreateMutexW(None, False, name)
        error = C.get_last_error()
        if not self.handle:
            self.close()
            raise C.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.kernel.SetEvent(self.activation)
            self.close()
            raise RuntimeError("Voicebar is already running; showing its bar")

    def activation_requested(self):
        return bool(self.activation and self.kernel.WaitForSingleObject(self.activation, 0) == 0)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.activation:
            self.kernel.CloseHandle(self.activation)
            self.activation = None


class HotkeyManager:
    """Three RegisterHotKey registrations on one private message-pump thread.

    GetAsyncKeyState's high bit detects release of the hold chord. This is
    intentional 10 ms polling, without a low-level hook or a listening socket.
    All registrations and unregisters occur on their owning worker thread.
    """

    def __init__(self, events: queue.Queue, *, toggle_listening="ctrl+alt+f9",
                 toggle_bar="ctrl+alt+f10", hold_to_talk="ctrl+alt+f8",
                 controller_factory=WindowsHotkey, copilot_key=False, copilot_factory=None):
        self.events = events
        self.shortcuts = {
            "toggle_listening": parse_hotkey(toggle_listening),
            "toggle_bar": parse_hotkey(toggle_bar),
            "hold_start": parse_hotkey(hold_to_talk),
        }
        chords = [(shortcut.modifiers, shortcut.key) for shortcut in self.shortcuts.values()]
        if len(set(chords)) != len(chords):
            raise PttError("The three bar hotkeys must be different")
        self._factory = controller_factory
        self.copilot_key, self._copilot_factory = copilot_key, copilot_factory
        self.copilot_error = None
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error: str | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("Bar hotkeys were already started")
        self._worker = threading.Thread(target=self._run, name="bar-hotkeys", daemon=True)
        self._worker.start()

    def close(self, timeout: float = 0) -> bool:
        """Request cleanup without blocking Qt; teardown may explicitly wait."""
        self._stop.set()
        if timeout and self._worker is not None:
            self._worker.join(timeout)
        return self._worker is None or self.stopped.is_set()

    def _run(self) -> None:
        controllers = {}
        copilot = None
        holding = False
        try:
            for index, (command, shortcut) in enumerate(self.shortcuts.items()):
                controller = self._factory(shortcut)
                controller.identifier = 0x5645 + index
                controllers[controller.identifier] = (command, controller)
                controller.register()
            if self.copilot_key:
                from .copilot import CopilotHook
                try:
                    copilot = (self._copilot_factory or CopilotHook)(self.events)
                    copilot.register()
                except Exception as error:  # noqa: BLE001 - optional key capture must preserve other controls
                    self.copilot_error = str(error)
                    self.events.put("copilot_error")
            self.ready.set()
            first = next(iter(controllers.values()))[1]
            message = first.message_type()
            hold = next(controller for command, controller in controllers.values()
                        if command == "hold_start")
            while not self._stop.is_set():
                while first.user32.PeekMessageW(C.byref(message), None, 0, 0, 1):
                    if message.message == 0x12:  # WM_QUIT
                        self._stop.set()
                        break
                    match = controllers.get(message.wParam) if message.message == 0x312 else None
                    if match is None:
                        continue
                    command, controller = match
                    if not controller.available():
                        continue
                    if command == "hold_start":
                        if not holding and controller.held():
                            holding = True
                            self.events.put("hold_start")
                    else:
                        self.events.put(command)
                if holding and (not hold.held() or not hold.available()):
                    holding = False
                    self.events.put("hold_end")
                self._stop.wait(0.01)
        except Exception as error:  # noqa: BLE001 - transfer worker failures to Qt via its queue.
            self.error = str(error)
            self.events.put("hotkey_error")
        finally:
            if copilot is not None:
                try:
                    copilot.close()
                except Exception as error:  # noqa: BLE001 - finish unregistering all keys on cleanup
                    self.error = self.error or str(error)
            if holding:
                self.events.put("hold_end")
            for _, controller in reversed(tuple(controllers.values())):
                try:
                    controller.close()
                except Exception as error:  # noqa: BLE001 - complete all native unregisters.
                    self.error = self.error or str(error)
            self.ready.set()
            self.stopped.set()


def _windows_dll(name: str):
    if os.name != "nt":
        raise RuntimeError("The bar requires native Windows")
    return C.WinDLL(name, use_last_error=True)


def apply_no_activate(hwnd: int, *, user32=None) -> int:
    """Apply the no-focus/tool-window contract to this application's own HWND."""
    user = user32 if user32 is not None else _windows_dll("user32")
    _bind(user, "GetWindowLongPtrW", LPARAM, HWND, C.c_int)
    _bind(user, "SetWindowLongPtrW", LPARAM, HWND, C.c_int, LPARAM)
    _bind(user, "SetWindowPos", BOOL, HWND, HWND, C.c_int, C.c_int, C.c_int, C.c_int, DWORD)
    C.set_last_error(0)
    original = user.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
    if original == 0 and C.get_last_error():
        raise C.WinError(C.get_last_error())
    style = (original | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    C.set_last_error(0)
    previous = user.SetWindowLongPtrW(hwnd, -20, style)
    if previous == 0 and C.get_last_error():
        raise C.WinError(C.get_last_error())
    # SWP_NOSIZE | NOMOVE | NOACTIVATE | FRAMECHANGED; HWND_TOPMOST.
    if not user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x01 | 0x02 | 0x10 | 0x20):
        raise C.WinError(C.get_last_error())
    return style


def handle_window_message(message: int, wparam: int) -> tuple[bool, int, str | None]:
    """Decode nativeEvent messages; display changes remain available to Qt."""
    if message == WM_MOUSEACTIVATE:
        return True, MA_NOACTIVATE, None
    if message == WM_POWERBROADCAST:
        if wparam == 4:  # PBT_APMSUSPEND
            return True, 1, "suspend"
        if wparam in (7, 18):  # PBT_APMRESUMESUSPEND / RESUMEAUTOMATIC
            return True, 1, "resume"
    if message == WM_WTSSESSION_CHANGE:
        if wparam == 7:  # WTS_SESSION_LOCK
            return True, 0, "locked"
        if wparam == 8:  # WTS_SESSION_UNLOCK
            return True, 0, "unlocked"
    if message == WM_DISPLAYCHANGE:
        return False, 0, "display_changed"
    return False, 0, None


class SessionNotifications:
    """Register only this user's session events for the bar's existing HWND."""

    def __init__(self, hwnd: int, *, wtsapi32=None):
        self.hwnd = hwnd
        self._wts = wtsapi32 if wtsapi32 is not None else _windows_dll("wtsapi32")
        _bind(self._wts, "WTSRegisterSessionNotification", BOOL, HWND, DWORD)
        _bind(self._wts, "WTSUnRegisterSessionNotification", BOOL, HWND)
        if not self._wts.WTSRegisterSessionNotification(hwnd, 0):  # NOTIFY_FOR_THIS_SESSION
            raise C.WinError(C.get_last_error())
        self._registered = True

    def close(self) -> None:
        if self._registered:
            if not self._wts.WTSUnRegisterSessionNotification(self.hwnd):
                raise C.WinError(C.get_last_error())
            self._registered = False


def _check_hresult(status: int, operation: str) -> None:
    if status < 0:
        raise OSError(f"{operation} failed (HRESULT 0x{status & 0xFFFFFFFF:08x})")


def _com_method(pointer, slot: int, result, *argument_types):
    table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result, C.c_void_p, *argument_types)(table[slot])


@contextmanager
def _shell_link():
    """Small IShellLinkW owner; balance COM only when we initialized it."""
    ole = _windows_dll("ole32")
    _bind(ole, "CoInitializeEx", HRESULT, HANDLE, DWORD)
    _bind(ole, "CoUninitialize", None)
    _bind(ole, "CoCreateInstance", HRESULT, C.POINTER(GUID), HANDLE, DWORD,
          C.POINTER(GUID), C.POINTER(HANDLE))
    initialized = ole.CoInitializeEx(None, 2)  # apartment-threaded
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE: existing apartment is usable.
        _check_hresult(initialized, "Initialize shortcut COM")
    link = HANDLE()
    try:
        clsid = _guid("00021401-0000-0000-C000-000000000046")  # CLSID_ShellLink
        iid = _guid("000214F9-0000-0000-C000-000000000046")  # IID_IShellLinkW
        _check_hresult(ole.CoCreateInstance(C.byref(clsid), None, 1, C.byref(iid), C.byref(link)),
                       "Create shortcut")
        yield link
    finally:
        if link:
            _com_method(link, 2, DWORD)(link)  # IUnknown::Release
        if initialized >= 0:
            ole.CoUninitialize()


def _create_shortcut(path: Path, target: Path, arguments: str, working_directory: Path) -> None:
    with _shell_link() as link:
        # Slot order verified against Microsoft's ShObjIdl_core.h.
        for slot, value in ((20, str(target)), (9, str(working_directory)),
                            (11, arguments), (7, "Voicebar local voice command bar")):
            _check_hresult(_com_method(link, slot, HRESULT, C.c_wchar_p)(link, value),
                           "Set shortcut property")
        _check_hresult(_com_method(link, 15, HRESULT, C.c_int)(link, 1), "Set shortcut show mode")
        persist = HANDLE()
        iid = _guid("0000010B-0000-0000-C000-000000000046")  # IID_IPersistFile
        _check_hresult(_com_method(link, 0, HRESULT, C.POINTER(GUID), C.POINTER(HANDLE))(
            link, C.byref(iid), C.byref(persist)), "Get shortcut persistence")
        try:
            _check_hresult(_com_method(persist, 6, HRESULT, C.c_wchar_p, BOOL)(
                persist, str(path), True), "Save shortcut")
        finally:
            _com_method(persist, 2, DWORD)(persist)


def _startup_folder() -> Path:
    shell, ole = _windows_dll("shell32"), _windows_dll("ole32")
    _bind(shell, "SHGetKnownFolderPath", HRESULT, C.POINTER(GUID), DWORD, HANDLE,
          C.POINTER(C.c_wchar_p))
    _bind(ole, "CoTaskMemFree", None, HANDLE)
    folder = _guid("B97D20BB-F46A-4C97-BA10-5E3608430854")  # FOLDERID_Startup (per user)
    result = C.c_wchar_p()
    try:
        _check_hresult(shell.SHGetKnownFolderPath(C.byref(folder), 0, None, C.byref(result)),
                       "Find per-user Startup folder")
        return Path(result.value)
    finally:
        if result:
            ole.CoTaskMemFree(C.cast(result, HANDLE))


class StartupShortcut:
    """Read-only until the user explicitly changes the tray's startup switch.

    The shortcut targets this environment's pythonw directly. It neither calls
    uv (which could download packages) nor starts a terminal, service, or shell.
    """

    def __init__(self, root: Path, *, startup_folder: Path | None = None,
                 executable: Path | None = None):
        self.root = Path(root).resolve()
        self.path = (startup_folder if startup_folder is not None else _startup_folder()) / "Voicebar.lnk"
        self.executable = (executable if executable is not None
                           else Path(sys.executable).with_name("pythonw.exe"))

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("Startup enabled must be a boolean")
        if not enabled:
            self.path.unlink(missing_ok=True)
            return
        if not self.executable.is_file():
            raise OSError(f"Windowless Python executable does not exist: {self.executable}")
        # Fixed code only; all paths are separate command-line data, never code.
        code = "from pathlib import Path; import sys; from voicebar.cli import main; raise SystemExit(main(['app'], project_root=Path(sys.argv[1])))"
        arguments = subprocess.list2cmdline(["-c", code, str(self.root)])
        temporary = self.path.with_name(self.path.stem + ".pending.lnk")
        try:
            _create_shortcut(temporary, self.executable, arguments, self.root)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
