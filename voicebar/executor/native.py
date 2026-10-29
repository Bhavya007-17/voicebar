"""Small, typed Win32 backend. Importing this module does not load native DLLs.

The public queries do not activate or modify a window. Mutating methods are
deliberately separate so the executor can validate ownership immediately first.
"""

from __future__ import annotations

import atexit
import ctypes as C
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TypeVar

DWORD = C.c_uint32
LONG = C.c_int32
UINT = C.c_uint32
BOOL = C.c_int32
WORD = C.c_uint16
HANDLE = C.c_void_p
HWND = HANDLE
WPARAM = C.c_size_t
LPARAM = C.c_ssize_t
HRESULT = C.c_int32
WINFUNCTYPE = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)

SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C


class POINT(C.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(C.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class WINDOWPLACEMENT(C.Structure):
    # rcDevice in some rendered documentation is guarded by _MAC in winuser.h.
    _fields_ = [
        ("length", UINT), ("flags", UINT), ("showCmd", UINT),
        ("ptMinPosition", POINT), ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class MONITORINFOEXW(C.Structure):
    _fields_ = [
        ("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
        ("dwFlags", DWORD), ("szDevice", C.c_wchar * 32),
    ]


class GUID(C.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", C.c_ubyte * 8)]


class PROPERTYKEY(C.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class _COUNTED_POINTER(C.Structure):
    _fields_ = [("count", DWORD), ("pointer", HANDLE)]


class _PROPVARIANT_VALUE(C.Union):
    _fields_ = [("pointer", HANDLE), ("uint64", C.c_uint64), ("counted", _COUNTED_POINTER)]


class PROPVARIANT(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", WORD), ("reserved1", WORD), ("reserved2", WORD), ("reserved3", WORD),
        ("value", _PROPVARIANT_VALUE),
    ]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", HANDLE), ("Attributes", DWORD)]


class TOKEN_MANDATORY_LABEL(C.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class GUITHREADINFO(C.Structure):
    _fields_ = [
        ("cbSize", DWORD), ("flags", DWORD), ("hwndActive", HWND),
        ("hwndFocus", HWND), ("hwndCapture", HWND), ("hwndMenuOwner", HWND),
        ("hwndMoveSize", HWND), ("hwndCaret", HWND), ("rcCaret", RECT),
    ]


class MSG(C.Structure):
    _fields_ = [
        ("hwnd", HWND), ("message", UINT), ("wParam", WPARAM),
        ("lParam", LPARAM), ("time", DWORD), ("pt", POINT), ("lPrivate", DWORD),
    ]


WNDENUMPROC = WINFUNCTYPE(BOOL, HWND, LPARAM)
MONITORENUMPROC = WINFUNCTYPE(BOOL, HANDLE, HANDLE, C.POINTER(RECT), LPARAM)
Rect = tuple[int, int, int, int]
T = TypeVar("T")


class NativeWaitTimeout(RuntimeError):
    """A state predicate stayed false until its deadline; native errors differ."""


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    tid: int
    title: str
    class_name: str
    exe: str
    aumids: tuple[str, ...]


@dataclass(frozen=True)
class Monitor:
    handle: int
    rect: Rect
    work: Rect
    primary: bool
    device: str
    dpi: int


def _rect(value: RECT) -> Rect:
    return value.left, value.top, value.right, value.bottom


def _guid(text: str) -> GUID:
    # UUID.bytes_le gives the Windows in-memory GUID representation.
    import uuid

    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class NativeWindows:
    """Windows 11 API adapter, constructed only when execution is requested."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Window execution requires native Windows, not WSL.")
        self.user32 = C.WinDLL("user32", use_last_error=True)
        self.kernel32 = C.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = C.WinDLL("advapi32", use_last_error=True)
        self.shell32 = C.WinDLL("shell32", use_last_error=True)
        self.ole32 = C.WinDLL("ole32", use_last_error=True)
        self.shlwapi = C.WinDLL("shlwapi", use_last_error=True)
        self.dwmapi = C.WinDLL("dwmapi", use_last_error=True)
        self.shcore = C.WinDLL("shcore", use_last_error=True)
        self._bind_functions()
        self._own_integrity: int | None = None
        self._com_state = threading.local()
        self._com_exit_registered = False

    def _bind_functions(self):
        u, k, a = self.user32, self.kernel32, self.advapi32
        _bind(u, "EnumWindows", BOOL, WNDENUMPROC, LPARAM)
        _bind(u, "EnumChildWindows", BOOL, HWND, WNDENUMPROC, LPARAM)
        _bind(u, "IsWindow", BOOL, HWND)
        _bind(u, "IsWindowVisible", BOOL, HWND)
        _bind(u, "IsIconic", BOOL, HWND)
        _bind(u, "IsChild", BOOL, HWND, HWND)
        _bind(u, "GetWindowLongPtrW", LPARAM, HWND, C.c_int)
        _bind(u, "GetWindowTextLengthW", C.c_int, HWND)
        _bind(u, "GetWindowTextW", C.c_int, HWND, C.c_wchar_p, C.c_int)
        _bind(u, "GetClassNameW", C.c_int, HWND, C.c_wchar_p, C.c_int)
        _bind(u, "GetWindowThreadProcessId", DWORD, HWND, C.POINTER(DWORD))
        _bind(u, "GetForegroundWindow", HWND)
        _bind(u, "SetForegroundWindow", BOOL, HWND)
        _bind(u, "BringWindowToTop", BOOL, HWND)
        _bind(u, "AttachThreadInput", BOOL, DWORD, DWORD, BOOL)
        _bind(u, "PeekMessageW", BOOL, C.POINTER(MSG), HWND, UINT, UINT, UINT)
        _bind(u, "GetWindowPlacement", BOOL, HWND, C.POINTER(WINDOWPLACEMENT))
        _bind(u, "GetWindowRect", BOOL, HWND, C.POINTER(RECT))
        _bind(u, "GetCursorPos", BOOL, C.POINTER(POINT))
        _bind(u, "MonitorFromPoint", HANDLE, POINT, DWORD)
        _bind(u, "MonitorFromWindow", HANDLE, HWND, DWORD)
        _bind(u, "EnumDisplayMonitors", BOOL, HANDLE, C.POINTER(RECT), MONITORENUMPROC, LPARAM)
        _bind(u, "GetMonitorInfoW", BOOL, HANDLE, C.POINTER(MONITORINFOEXW))
        _bind(u, "SetThreadDpiAwarenessContext", HANDLE, HANDLE)
        _bind(u, "ShowWindowAsync", BOOL, HWND, C.c_int)
        _bind(u, "SetWindowPos", BOOL, HWND, HWND, C.c_int, C.c_int, C.c_int, C.c_int, UINT)
        _bind(u, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM)
        _bind(u, "GetGUIThreadInfo", BOOL, DWORD, C.POINTER(GUITHREADINFO))
        _bind(u, "SendMessageTimeoutW", LPARAM, HWND, UINT, WPARAM, LPARAM, UINT, UINT,
              C.POINTER(WPARAM))
        _bind(k, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
        _bind(k, "CloseHandle", BOOL, HANDLE)
        _bind(k, "GetCurrentProcessId", DWORD)
        _bind(k, "GetCurrentThreadId", DWORD)
        _bind(k, "QueryFullProcessImageNameW", BOOL, HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD))
        _bind(k, "GetApplicationUserModelId", LONG, HANDLE, C.POINTER(UINT), C.c_wchar_p)
        _bind(a, "OpenProcessToken", BOOL, HANDLE, DWORD, C.POINTER(HANDLE))
        _bind(a, "GetTokenInformation", BOOL, HANDLE, C.c_int, HANDLE, DWORD, C.POINTER(DWORD))
        _bind(a, "GetSidSubAuthorityCount", C.POINTER(C.c_ubyte), HANDLE)
        _bind(a, "GetSidSubAuthority", C.POINTER(DWORD), HANDLE, DWORD)
        _bind(self.dwmapi, "DwmGetWindowAttribute", HRESULT, HWND, DWORD, HANDLE, DWORD)
        _bind(self.shcore, "GetScaleFactorForMonitor", HRESULT, HANDLE, C.POINTER(C.c_int))
        _bind(self.ole32, "CoInitializeEx", HRESULT, HANDLE, DWORD)
        _bind(self.ole32, "CoUninitialize", None)
        _bind(self.ole32, "PropVariantClear", HRESULT, C.POINTER(PROPVARIANT))
        _bind(self.shell32, "SHGetPropertyStoreForWindow", HRESULT, HWND, C.POINTER(GUID),
              C.POINTER(HANDLE))
        _bind(self.shlwapi, "AssocQueryStringW", HRESULT, DWORD, C.c_int, C.c_wchar_p,
              C.c_wchar_p, C.c_wchar_p, C.POINTER(DWORD))

    @staticmethod
    def _error(operation: str) -> RuntimeError:
        code = C.get_last_error()
        message = C.FormatError(code).strip() if code else "Windows did not report an error code"
        return RuntimeError(f"{operation} failed: {message} ({code}).")

    @contextmanager
    def _com(self):
        # Window property stores use COM. Keep the executor thread's apartment
        # alive across state queries and balance it once at thread shutdown.
        if not getattr(self._com_state, "initialized", False):
            status = self.ole32.CoInitializeEx(None, 2 | 4)  # STA, disable OLE1 DDE.
            if status < 0 and status != -2147417850:  # Existing MTA: RPC_E_CHANGED_MODE.
                raise RuntimeError(f"COM initialization failed (0x{status & 0xffffffff:08x}).")
            self._com_state.initialized = True
            self._com_state.owned = status >= 0
            if threading.current_thread() is threading.main_thread() and not self._com_exit_registered:
                atexit.register(self.shutdown)
                self._com_exit_registered = True
        yield

    def shutdown(self) -> None:
        """Balance this thread's apartment at executor/thread shutdown.

        Call on each worker thread before it exits; the main-thread CLI also has
        an atexit fallback. Never uninitialize another thread's apartment.
        """
        if getattr(self._com_state, "initialized", False):
            owned = self._com_state.owned
            self._com_state.initialized = False
            self._com_state.owned = False
            if owned:
                self.ole32.CoUninitialize()

    @contextmanager
    def _dpi(self):
        previous = self.user32.SetThreadDpiAwarenessContext(HANDLE(-4))
        if not previous:
            raise self._error("SetThreadDpiAwarenessContext")
        try:
            yield
        finally:
            self.user32.SetThreadDpiAwarenessContext(previous)

    def _pid_tid(self, hwnd: int) -> tuple[int, int]:
        pid = DWORD()
        tid = self.user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return int(pid.value), int(tid)

    def _title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        buffer = C.create_unicode_buffer(max(1, min(length + 1, 32768)))
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _class(self, hwnd: int) -> str:
        buffer = C.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def _process_identity(self, pid: int) -> tuple[str, str]:
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return "", ""
        try:
            size = DWORD(32768)
            buffer = C.create_unicode_buffer(size.value)
            exe = (buffer.value if self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, C.byref(size)
            ) else "")
            count = UINT()
            result = self.kernel32.GetApplicationUserModelId(handle, C.byref(count), None)
            aumid = ""
            if result == 122 and 0 < count.value <= 32768:
                app = C.create_unicode_buffer(count.value)
                if self.kernel32.GetApplicationUserModelId(handle, C.byref(count), app) == 0:
                    aumid = app.value
            return exe, aumid
        finally:
            self.kernel32.CloseHandle(handle)

    def _window_aumid(self, hwnd: int) -> str:
        interface = HANDLE()
        iid = _guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        key = PROPERTYKEY(_guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)
        value = PROPVARIANT()
        with self._com():
            result = self.shell32.SHGetPropertyStoreForWindow(hwnd, C.byref(iid),
                                                            C.byref(interface))
            if result < 0 or not interface:
                return ""
            table = C.cast(interface, C.POINTER(C.POINTER(HANDLE))).contents
            get_value = WINFUNCTYPE(HRESULT, HANDLE, C.POINTER(PROPERTYKEY),
                                    C.POINTER(PROPVARIANT))(table[5])
            release = WINFUNCTYPE(DWORD, HANDLE)(table[2])
            try:
                if (get_value(interface, C.byref(key), C.byref(value)) >= 0
                        and value.vt in (31, 8) and value.pointer):
                    return C.wstring_at(value.pointer)
                return ""
            finally:
                self.ole32.PropVariantClear(C.byref(value))
                release(interface)

    def children(self, hwnd: int) -> list[dict]:
        values: list[dict] = []

        @WNDENUMPROC
        def callback(child, _):
            pid, _tid = self._pid_tid(child)
            values.append({"hwnd": int(child), "pid": pid,
                           "class_name": self._class(child), "title": self._title(child)})
            return True

        self.user32.EnumChildWindows(hwnd, callback, 0)
        return values

    def windows(self) -> list[Window]:
        values: list[Window] = []
        identities: dict[int, tuple[str, str]] = {}
        callback_errors: list[Exception] = []

        def identity(pid):
            if pid not in identities:
                identities[pid] = self._process_identity(pid)
            return identities[pid]

        @WNDENUMPROC
        def callback(hwnd, _):
            try:
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                if self.user32.GetWindowLongPtrW(hwnd, -20) & 0x80:
                    return True
                title = self._title(hwnd)
                if not title:
                    return True
                cloaked = DWORD()
                if self.dwmapi.DwmGetWindowAttribute(hwnd, 14, C.byref(cloaked),
                                                    C.sizeof(cloaked)) >= 0 and cloaked.value:
                    return True
                pid, tid = self._pid_tid(hwnd)
                if not pid or not tid:
                    return True
                exe, process_id = identity(pid)
                class_name = self._class(hwnd)
                app_ids = {process_id, self._window_aumid(hwnd)} - {""}
                if (PureWindowsPath(exe).name.casefold() == "applicationframehost.exe"
                        or class_name == "ApplicationFrameWindow"):
                    for child in self.children(hwnd):
                        _child_exe, child_id = identity(child["pid"])
                        if child_id:
                            app_ids.add(child_id)
                values.append(Window(int(hwnd), pid, tid, title, class_name, exe,
                                     tuple(sorted(app_ids))))
            except Exception as error:  # noqa: BLE001 - propagate outside the ctypes callback.
                # Never let ctypes swallow a callback exception as apparent success.
                callback_errors.append(error)
                return False
            return True

        succeeded = self.user32.EnumWindows(callback, 0)
        if callback_errors:
            raise RuntimeError(f"Window enumeration failed: {callback_errors[0]}")
        if not succeeded:
            raise self._error("EnumWindows")
        return values

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def is_window(self, hwnd: int, pid: int) -> bool:
        return bool(pid > 0 and self.user32.IsWindow(hwnd) and self._pid_tid(hwnd)[0] == pid)

    def integrity(self, pid: int) -> int:
        process = self.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            raise self._error("Inspecting target process integrity")
        token = HANDLE()
        try:
            if not self.advapi32.OpenProcessToken(process, 8, C.byref(token)):
                raise self._error("OpenProcessToken")
            size = DWORD()
            self.advapi32.GetTokenInformation(token, 25, None, 0, C.byref(size))
            if size.value < C.sizeof(TOKEN_MANDATORY_LABEL) or size.value > 65536:
                raise self._error("Querying integrity information size")
            buffer = C.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(token, 25, buffer, size, C.byref(size)):
                raise self._error("GetTokenInformation(TokenIntegrityLevel)")
            label = C.cast(buffer, C.POINTER(TOKEN_MANDATORY_LABEL)).contents
            count = self.advapi32.GetSidSubAuthorityCount(label.Label.Sid)
            if not count or not count[0]:
                raise RuntimeError("Target integrity SID is missing its authority.")
            rid = self.advapi32.GetSidSubAuthority(label.Label.Sid, count[0] - 1)
            if not rid:
                raise RuntimeError("Target integrity RID could not be read.")
            return int(rid[0])
        finally:
            if token:
                self.kernel32.CloseHandle(token)
            self.kernel32.CloseHandle(process)

    def current_integrity(self) -> int:
        if self._own_integrity is None:
            self._own_integrity = self.integrity(self.kernel32.GetCurrentProcessId())
        return self._own_integrity

    @staticmethod
    def wait(predicate: Callable[[], T], timeout: float, description: str) -> T:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeWaitTimeout(f"Timed out waiting for {description} ({timeout:g} s).")
            time.sleep(min(0.01, remaining))  # Poll a state predicate, never hide an action race.

    def activate(self, hwnd: int, timeout: float) -> None:
        if not self.user32.IsWindow(hwnd):
            raise RuntimeError("The target window no longer exists.")
        if self.user32.IsIconic(hwnd):
            self.show(hwnd, SW_RESTORE)
            self.wait(lambda: not self.user32.IsIconic(hwnd), timeout, "window restoration")
        if self.foreground() == hwnd:
            return
        self.user32.SetForegroundWindow(hwnd)
        if self.foreground() == hwnd:
            return
        # PeekMessage creates the caller's input queue without consuming messages.
        message = MSG()
        self.user32.PeekMessageW(C.byref(message), None, 0, 0, 0)
        current = int(self.kernel32.GetCurrentThreadId())
        foreground_tid = self._pid_tid(self.foreground())[1] if self.foreground() else 0
        target_tid = self._pid_tid(hwnd)[1]
        attached: list[tuple[int, int]] = []
        try:
            for tid in dict.fromkeys((foreground_tid, target_tid)):
                if tid and tid != current and self.user32.AttachThreadInput(current, tid, True):
                    attached.append((current, tid))
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
        finally:
            for source, destination in reversed(attached):
                self.user32.AttachThreadInput(source, destination, False)
        try:
            self.wait(lambda: self.foreground() == hwnd, min(timeout, 2.0), "foreground activation")
        except RuntimeError as error:
            raise RuntimeError("Windows refused foreground activation; an active menu, foreground "
                               "lock, another desktop, or concurrent input can prevent it.") from error

    def placement(self, hwnd: int) -> dict:
        value = WINDOWPLACEMENT()
        value.length = C.sizeof(value)
        with self._dpi():
            if not self.user32.GetWindowPlacement(hwnd, C.byref(value)):
                raise self._error("GetWindowPlacement")
        return {"show": int(value.showCmd), "rect": _rect(value.rcNormalPosition)}

    def rect(self, hwnd: int) -> Rect:
        value = RECT()
        with self._dpi():
            if not self.user32.GetWindowRect(hwnd, C.byref(value)):
                raise self._error("GetWindowRect")
        return _rect(value)

    def monitor(self, hwnd: int) -> int:
        with self._dpi():
            handle = self.user32.MonitorFromWindow(hwnd, 2)  # nearest
        if not handle:
            raise self._error("MonitorFromWindow")
        return int(handle)

    def cursor_monitor(self) -> int:
        """Read the display containing the mouse; never guess across a gap."""
        point = POINT()
        with self._dpi():
            if not self.user32.GetCursorPos(C.byref(point)):
                raise self._error("GetCursorPos")
            handle = self.user32.MonitorFromPoint(point, 0)  # MONITOR_DEFAULTTONULL
        if not handle:
            raise RuntimeError("The mouse cursor is not on an identified display.")
        return int(handle)

    def monitors(self) -> list[Monitor]:
        values: list[Monitor] = []
        errors: list[Exception] = []

        @MONITORENUMPROC
        def callback(handle, _hdc, _rect_pointer, _):
            try:
                info = MONITORINFOEXW()
                info.cbSize = C.sizeof(info)
                if not self.user32.GetMonitorInfoW(handle, C.byref(info)):
                    raise self._error("GetMonitorInfoW")
                scale = C.c_int(100)
                status = self.shcore.GetScaleFactorForMonitor(handle, C.byref(scale))
                if status < 0 or scale.value <= 0:
                    raise RuntimeError("The display's scaling factor could not be read.")
                values.append(Monitor(int(handle), _rect(info.rcMonitor), _rect(info.rcWork),
                                      bool(info.dwFlags & 1), info.szDevice,
                                      round(96 * scale.value / 100)))
            except Exception as error:  # noqa: BLE001 - propagate outside the ctypes callback.
                errors.append(error)
                return False
            return True

        with self._dpi():
            succeeded = self.user32.EnumDisplayMonitors(None, None, callback, 0)
        if errors:
            raise errors[0]
        if not succeeded:
            raise self._error("EnumDisplayMonitors")
        return sorted(values, key=lambda value: (value.rect[0], value.rect[1], value.device))

    def show(self, hwnd: int, cmd: int) -> None:
        if not self.user32.ShowWindowAsync(hwnd, cmd):
            raise self._error("ShowWindowAsync")

    def position(self, hwnd: int, rect: Rect) -> None:
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            raise RuntimeError("The requested window rectangle is empty.")
        with self._dpi():
            # Preserve z-order, avoid stealing focus, and post across input queues.
            if not self.user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                                           0x0004 | 0x0010 | 0x4000):
                raise self._error("SetWindowPos")

    def close(self, hwnd: int) -> None:
        if not self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise self._error("Posting WM_CLOSE")

    def launch_app(self, app_id: str) -> None:
        if not isinstance(app_id, str) or not app_id or "\0" in app_id:
            raise RuntimeError("The installed app has an invalid AppID.")
        # Microsoft's documented AppsFolder activation route. Delegate package
        # activation to the Windows shell instead of hosting shell extensions
        # and their COM rundown inside the executor process.
        explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
        subprocess.Popen([str(explorer), "shell:AppsFolder\\" + app_id], shell=False,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def default_browser(self, scheme: str = "https") -> str:
        if scheme not in ("http", "https"):
            raise RuntimeError("Only HTTP and HTTPS browser associations are supported.")
        length = DWORD()
        status = self.shlwapi.AssocQueryStringW(0x1000 | 0x20, 2, scheme, "open", None,
                                              C.byref(length))
        if status not in (0, 1) or not 0 < length.value <= 32768:
            raise RuntimeError("Windows could not resolve the default browser executable.")
        for _ in range(3):
            buffer = C.create_unicode_buffer(length.value)
            status = self.shlwapi.AssocQueryStringW(0x1000 | 0x20, 2, scheme, "open", buffer,
                                                  C.byref(length))
            if status == 0 and buffer.value:
                return buffer.value
            if (status & 0xffffffff) != 0x80004003 or not 0 < length.value <= 32768:
                break
        raise RuntimeError("The default browser association has no usable executable.")

    def _edit_control(self, hwnd: int) -> int:
        def readable(candidate):
            name = self._class(candidate).casefold()
            return name == "edit" or name.startswith("richedit")

        pid, tid = self._pid_tid(hwnd)
        if not pid:
            raise RuntimeError("The text target no longer exists.")
        gui = GUITHREADINFO()
        gui.cbSize = C.sizeof(gui)
        if self.user32.GetGUIThreadInfo(tid, C.byref(gui)) and gui.hwndFocus:
            focus = int(gui.hwndFocus)
            if (focus == hwnd or self.user32.IsChild(hwnd, focus)) and readable(focus):
                return focus
        if readable(hwnd):
            return hwnd
        for child in self.children(hwnd):
            if readable(child["hwnd"]):
                return child["hwnd"]
        raise RuntimeError("This window has no readable standard edit control.")

    def _message(self, hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
        result = WPARAM()
        C.set_last_error(0)
        if not self.user32.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x02 | 0x20,
                                              2000, C.byref(result)):
            raise self._error("Sending a bounded edit-control message")
        return int(result.value)

    def read_text(self, hwnd: int) -> str:
        child = self._edit_control(hwnd)
        size = self._message(child, WM_GETTEXTLENGTH) + 1
        if size > 1_048_576:
            raise RuntimeError("The edit control is too large for a verification read.")
        buffer = C.create_unicode_buffer(max(1, size))
        self._message(child, WM_GETTEXT, len(buffer), C.addressof(buffer))
        return buffer.value

    def clear_text(self, hwnd: int) -> None:
        # Reserved for cleanup of a caller-owned test edit, never used by close_app.
        pid, _ = self._pid_tid(hwnd)
        if not pid or self.integrity(pid) > self.current_integrity():
            raise RuntimeError("The edit target cannot be modified at this integrity level.")
        child = self._edit_control(hwnd)
        empty = C.create_unicode_buffer(1)
        if not self._message(child, WM_SETTEXT, 0, C.addressof(empty)):
            raise RuntimeError("The edit control refused to clear its text.")
        self.wait(lambda: self.read_text(hwnd) == "", 2.0, "test text to be cleared")
