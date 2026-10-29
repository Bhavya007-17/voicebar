"""Guarded Windows input, UTF-16 text, clipboard paste, and media keys.

Clipboard paste is deliberately limited to synchronous Edit/RichEdit controls.
Unknown clipboard handle types are refused before the clipboard is changed.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from uuid import UUID

from .common import ExecutionError

KEYUP, UNICODE, EXTENDED = 2, 4, 1
MODIFIERS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
VKS = {
    **MODIFIERS, "enter": 0x0D, "esc": 0x1B, "tab": 9, "space": 0x20,
    "backspace": 8, "delete": 0x2E, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
}
EXTENDED_VKS = {0x5B, 0x5C, 0x2E, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22}


class UnsupportedEditControl(ExecutionError):
    """The focused framework has no documented standard edit-message interface."""


class KEYBDINPUT(C.Structure):
    _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", C.c_size_t)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD),
                ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("message", W.DWORD), ("low", W.WORD), ("high", W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("keyboard", KEYBDINPUT), ("mouse", MOUSEINPUT), ("hardware", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", W.DWORD), ("value", INPUTUNION)]


class GUITHREADINFO(C.Structure):
    _fields_ = [("size", W.DWORD), ("flags", W.DWORD), ("active", W.HWND),
                ("focus", W.HWND), ("capture", W.HWND), ("menu", W.HWND),
                ("move", W.HWND), ("caret", W.HWND), ("rect", W.RECT)]


class METAFILEPICT(C.Structure):
    _fields_ = [("mode", C.c_int), ("x", C.c_int), ("y", C.c_int), ("metafile", W.HANDLE)]


def key_event(vk: int, up: bool = False) -> tuple[int, int, int, int]:
    return (1, vk, 0, (EXTENDED if vk in EXTENDED_VKS else 0) | (KEYUP if up else 0))


def key_events(keys: str) -> list[tuple[int, int, int, int]]:
    """Resolve the parser's canonical keys; refuse unsupported physical F keys."""
    codes = []
    for key in keys.split("+"):
        if key in VKS:
            vk = VKS[key]
        elif len(key) == 1 and key.isascii() and key.isalnum():
            vk = ord(key.upper())
        elif key.startswith("f") and key[1:].isascii() and key[1:].isdigit():
            number = int(key[1:])
            if not 1 <= number <= 24:
                raise ExecutionError("Windows supports F1 through F24; this function key is unsupported")
            vk = 0x70 + number - 1
        else:
            raise ExecutionError(f"Unsupported physical key: {key}")
        if vk in codes:
            raise ExecutionError("A shortcut cannot hold the same key twice")
        codes.append(vk)
    # Preserve a key chord's declared order and release every injected key in reverse.
    return [key_event(vk) for vk in codes] + [key_event(vk, True) for vk in reversed(codes)]


def unicode_events(text: str) -> list[tuple[int, int, int, int]]:
    """Use both UTF-16 units for supplementary characters; line breaks use Enter."""
    events = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for char in normalized:
        if char == "\n":
            events += [key_event(0x0D), key_event(0x0D, True)]
            continue
        raw = char.encode("utf-16-le", errors="strict")
        for index in range(0, len(raw), 2):
            unit = int.from_bytes(raw[index:index + 2], "little")
            events += [(1, 0, unit, UNICODE), (1, 0, unit, UNICODE | KEYUP)]
    return events


def _api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


class InputBackend:
    """Lazy ctypes boundary. Fake backends replace this whole object in unit tests."""

    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.ole = C.WinDLL("ole32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        if C.sizeof(INPUT) != 40:
            raise ExecutionError("This input backend requires native Windows x64 INPUT layout")
        self.send_input = _api(self.user, "SendInput", W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)
        self.async_key = _api(self.user, "GetAsyncKeyState", C.c_short, C.c_int)
        _api(self.user, "GetGUIThreadInfo", W.BOOL, W.DWORD, C.POINTER(GUITHREADINFO))
        _api(self.user, "GetAncestor", W.HWND, W.HWND, W.UINT)
        _api(self.user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
        _api(self.user, "WindowFromPoint", W.HWND, W.POINT)
        _api(self.user, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        _api(self.user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        _api(self.user, "SendMessageTimeoutW", W.LPARAM, W.HWND, W.UINT, W.WPARAM,
             W.LPARAM, W.UINT, W.UINT, C.POINTER(C.c_size_t))
        _api(self.user, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR,
             W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
        _api(self.user, "DestroyWindow", W.BOOL, W.HWND)
        _api(self.user, "OpenClipboard", W.BOOL, W.HWND)
        _api(self.user, "CloseClipboard", W.BOOL)
        _api(self.user, "EmptyClipboard", W.BOOL)
        _api(self.user, "EnumClipboardFormats", W.UINT, W.UINT)
        _api(self.user, "GetClipboardData", W.HANDLE, W.UINT)
        _api(self.user, "SetClipboardData", W.HANDLE, W.UINT, W.HANDLE)
        _api(self.user, "GetClipboardSequenceNumber", W.DWORD)
        _api(self.user, "GetClipboardOwner", W.HWND)
        _api(self.kernel, "GlobalAlloc", W.HANDLE, W.UINT, C.c_size_t)
        _api(self.kernel, "GlobalLock", W.LPVOID, W.HANDLE)
        _api(self.kernel, "GlobalUnlock", W.BOOL, W.HANDLE)
        _api(self.kernel, "GlobalFree", W.HANDLE, W.HANDLE)
        _api(self.ole, "OleDuplicateData", W.HANDLE, W.HANDLE, W.WORD, W.UINT)
        _api(self.gdi, "CopyEnhMetaFileW", W.HANDLE, W.HANDLE, W.LPCWSTR)
        _api(self.gdi, "DeleteEnhMetaFile", W.BOOL, W.HANDLE)
        _api(self.gdi, "DeleteMetaFile", W.BOOL, W.HANDLE)
        _api(self.gdi, "DeleteObject", W.BOOL, W.HANDLE)

    def modifiers_held(self) -> bool:
        return any(self.async_key(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def wheel_target(self, window):
        point = W.POINT()
        if not self.user.GetCursorPos(C.byref(point)):
            raise ExecutionError("Cannot determine the wheel input target")
        hover = self.user.WindowFromPoint(point)
        return int(self.user.GetAncestor(hover, 2) or 0) == window.hwnd

    @staticmethod
    def _array(events):
        array = (INPUT * len(events))()
        for item, (kind, vk, scan, flags) in zip(array, events, strict=True):
            item.type = kind
            if kind == 1:
                item.keyboard = KEYBDINPUT(vk, scan, flags, 0, 0)
            else:
                item.mouse = MOUSEINPUT(0, 0, scan & 0xFFFFFFFF, flags, 0, 0)
        return array

    def send(self, events):
        array = self._array(events)
        count = int(self.send_input(len(events), array, C.sizeof(INPUT)))
        if count != len(events):
            # Only release keys this batch actually pressed; never release user's keys.
            pressed = {}
            for kind, vk, scan, flags in events[:count]:
                if kind != 1:
                    continue
                identity = (vk, scan, flags & ~KEYUP)
                if flags & KEYUP:
                    pressed.pop(identity, None)
                else:
                    pressed[identity] = (kind, vk, scan, flags | KEYUP)
            releases = list(reversed(list(pressed.values())))
            if releases:
                cleanup = self._array(releases)
                released = int(self.send_input(len(releases), cleanup, C.sizeof(INPUT)))
                if released != len(releases):
                    raise ExecutionError("SendInput failed and could not release every injected key")
            raise ExecutionError(f"SendInput inserted {count}/{len(events)} events; action incomplete")

    def _focus(self, window):
        info = GUITHREADINFO(size=C.sizeof(GUITHREADINFO))
        if not self.user.GetGUIThreadInfo(0, C.byref(info)) or not info.focus:
            raise ExecutionError("Cannot determine the focused edit control for clipboard paste")
        if int(self.user.GetAncestor(info.focus, 2) or 0) != window.hwnd:
            raise ExecutionError("Focused control no longer belongs to the guarded window")
        focus = int(info.focus)
        name = C.create_unicode_buffer(256)
        if not self.user.GetClassNameW(focus, name, len(name)):
            raise ExecutionError("Cannot identify the focused control")
        if name.value.casefold() != "edit" and not name.value.casefold().startswith("richedit"):
            from .focus import FocusReader
            if getattr(self, "focus_reader", None) is None:
                self.focus_reader = FocusReader()
            actual = self.focus_reader.focused()
            if not actual or not actual[0] or not actual[2] or actual[3]:
                raise UnsupportedEditControl("Focused UI Automation element has no usable non-password edit HWND")
            focus, expected_pid, _, _ = actual
            if int(self.user.GetAncestor(focus, 2) or 0) != window.hwnd:
                raise ExecutionError("UI Automation focus changed to another window; input refused")
            if not self.user.GetClassNameW(focus, name, len(name)):
                raise ExecutionError("Cannot identify the UI Automation focused control")
            if name.value.casefold() != "edit" and not name.value.casefold().startswith("richedit"):
                raise UnsupportedEditControl("Text messages require a focused Edit/RichEdit control; unsupported framework")
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(focus, C.byref(pid))
            if pid.value != expected_pid:
                raise ExecutionError("UI Automation focused control identity changed")
            return focus, int(pid.value)
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(focus, C.byref(pid))
        return focus, int(pid.value)

    def close(self):
        reader = getattr(self, "focus_reader", None)
        if reader is not None:
            reader.close()
            self.focus_reader = None

    def _selection_units(self, focus, text):
        # RichEdit positions count an internal CR as one UTF-16 unit, while
        # WM_GETTEXT expands it to CRLF. Plain Edit positions count both.
        if hasattr(self.user, "GetClassNameW"):
            name = C.create_unicode_buffer(256)
            if not self.user.GetClassNameW(focus, name, len(name)):
                raise ExecutionError("Cannot identify edit selection coordinates")
            if name.value.casefold().startswith("richedit"):
                text = text.replace("\r\n", "\r")
        return text.encode("utf-16-le", errors="surrogatepass")

    def selection(self, window):
        focus, _ = self._focus(window)
        start, end, result = W.DWORD(), W.DWORD(), C.c_size_t()
        if not self.user.SendMessageTimeoutW(focus, 0xB0, C.addressof(start), C.addressof(end),
                                             0x23, 2000, C.byref(result)):
            raise ExecutionError("Cannot query edit selection")
        return start.value, end.value

    def replace_text(self, ctx, window, text):
        """Replace the current selection synchronously, preserving normal undo behavior."""
        try:
            focus, pid = self._focus(window)
        except UnsupportedEditControl:
            return False
        if ctx.native.integrity(pid) > ctx.native.current_integrity():
            raise ExecutionError("Focused edit control is elevated; text insertion refused")
        original = ctx.native.read_text(window.hwnd)
        start, end, result = W.DWORD(), W.DWORD(), C.c_size_t()
        timeout = min(int(ctx.timeout * 1000), 30000)
        if not self.user.SendMessageTimeoutW(focus, 0xB0, C.addressof(start), C.addressof(end),
                                             0x23, timeout, C.byref(result)):
            raise ExecutionError("Cannot read the edit selection; text insertion refused")
        original_units = self._selection_units(focus, original)
        if not 0 <= start.value <= end.value <= len(original_units) // 2:
            raise ExecutionError("The edit selection changed or is outside the readable text")
        replacement = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        expected = (original_units[:start.value * 2] + replacement.encode("utf-16-le")
                    + original_units[end.value * 2:]).decode("utf-16-le", errors="surrogatepass")
        buffer = C.create_unicode_buffer(replacement)
        _foreground_guard(ctx, window)
        if self._focus(window) != (focus, pid) or not ctx.native.is_window(focus, pid):
            raise ExecutionError("Focused edit control changed before text insertion")
        ctx.begin()
        # EM_REPLACESEL has no result value. TRUE wParam keeps the operation undoable.
        if not self.user.SendMessageTimeoutW(focus, 0xC2, 1, C.addressof(buffer),
                                             0x23, timeout, C.byref(result)):
            raise ExecutionError("Edit control did not complete synchronous text insertion")

        def canonical(value):
            return value.replace("\r\n", "\n").replace("\r", "\n")

        ctx.wait(lambda: canonical(ctx.native.read_text(window.hwnd)) == canonical(expected),
                 "edit selection replacement text")
        return True

    @staticmethod
    def _supported_format(fmt):
        return fmt != 0x80 and not 0x200 <= fmt <= 0x3FF and (fmt <= 17 or fmt in {0x81, 0x82, 0x83, 0x8E} or fmt >= 0xC000)

    def _free(self, fmt, handle):
        if fmt in {2, 9, 0x82}:
            self.gdi.DeleteObject(handle)
        elif fmt in {14, 0x8E}:
            self.gdi.DeleteEnhMetaFile(handle)
        elif fmt in {3, 0x83}:
            pointer = self.kernel.GlobalLock(handle)
            if pointer:
                metafile = C.cast(pointer, C.POINTER(METAFILEPICT)).contents.metafile
                self.gdi.DeleteMetaFile(metafile)
                self.kernel.GlobalUnlock(handle)
            self.kernel.GlobalFree(handle)
        else:
            self.kernel.GlobalFree(handle)

    def _snapshot(self):
        saved = []
        try:
            fmt = 0
            while True:
                C.set_last_error(0)
                fmt = int(self.user.EnumClipboardFormats(fmt))
                if not fmt:
                    if C.get_last_error():
                        raise ExecutionError("Cannot enumerate all clipboard formats; clipboard unchanged")
                    return saved
                if not self._supported_format(fmt):
                    raise ExecutionError(f"Clipboard format {fmt} cannot be safely preserved; clipboard unchanged")
                handle = self.user.GetClipboardData(fmt)
                if not handle:
                    raise ExecutionError(f"Cannot materialize clipboard format {fmt}; clipboard unchanged")
                duplicate_format = {0x82: 2, 0x83: 3}.get(fmt, fmt)
                duplicate = (self.gdi.CopyEnhMetaFileW(handle, None) if fmt in {14, 0x8E}
                             else self.ole.OleDuplicateData(handle, duplicate_format, 0))
                if not duplicate:
                    raise ExecutionError(f"Cannot duplicate clipboard format {fmt}; clipboard unchanged")
                saved.append((fmt, duplicate))
        except Exception:
            for fmt, handle in saved:
                self._free(fmt, handle)
            raise

    def paste(self, ctx, window, text):
        focus, pid = self._focus(window)
        if ctx.native.integrity(pid) > ctx.native.current_integrity():
            raise ExecutionError("Focused edit control is elevated; clipboard paste refused")
        original = ctx.native.read_text(window.hwnd)
        start, end, selection = W.DWORD(), W.DWORD(), C.c_size_t()
        if not self.user.SendMessageTimeoutW(focus, 0xB0, C.addressof(start), C.addressof(end),
                                             0x23, min(int(ctx.timeout * 1000), 30000), C.byref(selection)):
            raise ExecutionError("Cannot read paste selection; clipboard unchanged")
        units = self._selection_units(focus, original)
        if not 0 <= start.value <= end.value <= len(units) // 2:
            raise ExecutionError("Paste selection is outside readable text; clipboard unchanged")
        expected = (units[:start.value * 2] + text.encode("utf-16-le")
                    + units[end.value * 2:]).decode("utf-16-le", errors="surrogatepass")
        def canonical(value):
            return value.replace("\r\n", "\n").replace("\r", "\n")
        ctx.begin()
        owner = self.user.CreateWindowExW(0, "STATIC", "Voicebar clipboard owner", 0,
                                          0, 0, 0, 0, None, None, None, None)
        if not owner:
            raise ExecutionError("Cannot create clipboard owner window")
        saved, sequence, changed = [], None, False
        try:
            if not self.user.OpenClipboard(owner):
                raise ExecutionError("Clipboard is busy; paste refused")
            try:
                saved = self._snapshot()
                raw = text.encode("utf-16-le") + b"\0\0"
                memory = self.kernel.GlobalAlloc(2, len(raw))
                if not memory:
                    raise ExecutionError("Cannot allocate clipboard text")
                pointer = self.kernel.GlobalLock(memory)
                if not pointer:
                    self.kernel.GlobalFree(memory)
                    raise ExecutionError("Cannot lock clipboard text")
                C.memmove(pointer, raw, len(raw))
                self.kernel.GlobalUnlock(memory)
                if not self.user.EmptyClipboard():
                    self.kernel.GlobalFree(memory)
                    raise ExecutionError("Cannot prepare clipboard for paste")
                changed = True
                sequence = int(self.user.GetClipboardSequenceNumber())
                if not self.user.SetClipboardData(13, memory):
                    self.kernel.GlobalFree(memory)
                    raise ExecutionError("Cannot publish clipboard text")
                sequence = int(self.user.GetClipboardSequenceNumber())
            finally:
                self.user.CloseClipboard()
            # Closing the publication adds Windows' synthesized text formats.
            # Materialize them before recording our sequence: their first read
            # can increment it even though nobody copied new data. Verify both
            # ownership and the published Unicode handle while holding the lock.
            if not self.user.OpenClipboard(owner):
                raise ExecutionError("Clipboard became busy before paste")
            try:
                if int(self.user.GetClipboardOwner() or 0) != int(owner) or self.user.GetClipboardData(13) != memory:
                    raise ExecutionError("Clipboard changed before paste; input refused")
                fmt = 0
                while (fmt := int(self.user.EnumClipboardFormats(fmt))):
                    if fmt not in {1, 7, 13, 16} or not self.user.GetClipboardData(fmt):
                        raise ExecutionError("Clipboard changed before paste; input refused")
                sequence = int(self.user.GetClipboardSequenceNumber())
            finally:
                self.user.CloseClipboard()
            _foreground_guard(ctx, window)
            if self._focus(window)[0] != focus:
                raise ExecutionError("Focused edit control changed before paste")
            if self.modifiers_held():
                raise ExecutionError("A physical modifier key is held; paste refused")
            self.send(key_events("ctrl+v"))
            # Route through the application's paste command; modern Notepad can
            # ignore direct WM_PASTE on its internal RichEdit. Keep clipboard
            # data until readback proves the application's paste completed.
            try:
                ctx.wait(lambda: canonical(ctx.native.read_text(window.hwnd)) == canonical(expected),
                         "pasted text consumption before clipboard restoration")
            except RuntimeError as error:
                observed = canonical(ctx.native.read_text(window.hwnd))
                wanted = canonical(expected)
                different = next((index for index, pair in enumerate(zip(observed, wanted))
                                  if pair[0] != pair[1]), min(len(observed), len(wanted)))
                raise ExecutionError(f"Paste unconfirmed: expected {len(wanted)} characters, "
                                     f"read {len(observed)}, first difference at {different}; {error}") from error
        finally:
            try:
                if changed and (sequence is None or self.user.GetClipboardSequenceNumber() == sequence):
                    # Text readback can complete just before the target releases
                    # its clipboard lock. Wait on that actual lock, then recheck
                    # sequence while holding it; a concurrent copy still wins.
                    ctx.wait(lambda: bool(self.user.OpenClipboard(owner)), "clipboard access for restoration")
                    try:
                        # Recheck after acquiring the clipboard: a concurrent user copy wins.
                        if sequence is None or self.user.GetClipboardSequenceNumber() == sequence:
                            if not self.user.EmptyClipboard():
                                raise ExecutionError("Cannot restore saved clipboard")
                            for fmt, handle in list(saved):
                                if not self.user.SetClipboardData(fmt, handle):
                                    raise ExecutionError(f"Cannot restore clipboard format {fmt}")
                                saved.remove((fmt, handle))
                    finally:
                        self.user.CloseClipboard()
            finally:
                for fmt, handle in saved:
                    self._free(fmt, handle)
                self.user.DestroyWindow(owner)

    def audio_state(self):
        """Read default render endpoint mute and scalar volume through Core Audio."""
        ole = self.ole
        _api(ole, "CoInitializeEx", C.c_long, W.LPVOID, W.DWORD)
        _api(ole, "CoUninitialize", None)
        _api(ole, "CoCreateInstance", C.c_long, W.LPVOID, W.LPVOID, W.DWORD, W.LPVOID, C.POINTER(W.LPVOID))
        initialized = ole.CoInitializeEx(None, 2)
        if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE: existing COM apartment.
            raise ExecutionError("Cannot initialize Core Audio query")
        pointers = []

        def guid(value):
            return C.create_string_buffer(UUID(value).bytes_le, 16)

        def call(pointer, slot, *types):
            table = C.cast(pointer, C.POINTER(C.POINTER(W.LPVOID))).contents
            return C.WINFUNCTYPE(C.c_long, W.LPVOID, *types)(table[slot])

        def check(result):
            if result < 0:
                raise ExecutionError(f"Core Audio query failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")

        try:
            enumerator = W.LPVOID()
            check(ole.CoCreateInstance(guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), None, 1,
                                       guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), C.byref(enumerator)))
            pointers.append(enumerator)
            device = W.LPVOID()
            check(call(enumerator, 4, C.c_int, C.c_int, C.POINTER(W.LPVOID))(
                enumerator, 0, 0, C.byref(device)))
            pointers.append(device)
            endpoint = W.LPVOID()
            check(call(device, 3, W.LPVOID, W.DWORD, W.LPVOID, C.POINTER(W.LPVOID))(
                device, guid("5CDF2C82-841E-4546-9722-0CF74078229A"), 23, None, C.byref(endpoint)))
            pointers.append(endpoint)
            mute, level = W.BOOL(), C.c_float()
            check(call(endpoint, 15, C.POINTER(W.BOOL))(endpoint, C.byref(mute)))
            check(call(endpoint, 9, C.POINTER(C.c_float))(endpoint, C.byref(level)))
            return bool(mute.value), float(level.value)
        finally:
            for pointer in reversed(pointers):
                call(pointer, 2)(pointer)
            if initialized >= 0:
                ole.CoUninitialize()


def _backend(ctx):
    if getattr(ctx, "input_backend", None) is None:
        ctx.input_backend = InputBackend()
    return ctx.input_backend


def _foreground_guard(ctx, window):
    ctx.guard(window)
    if ctx.native.foreground() != window.hwnd:
        raise ExecutionError("Foreground changed; input refused")


def _send(ctx, window, events):
    backend = _backend(ctx)
    if backend.modifiers_held():
        raise ExecutionError("A physical modifier key is held; input refused")
    _foreground_guard(ctx, window)
    ctx.begin()
    backend.send(events)


def press(ctx, action):
    return press_window(ctx, ctx.target(), action["keys"])


def press_window(ctx, window, keys):
    """Send to the already verified target; refuse any intervening foreground change."""
    _send(ctx, window, key_events(keys))
    if getattr(window, "class_name", "") == "Notepad" and keys == "ctrl+a":
        backend = _backend(ctx)
        focus, _ = backend._focus(window)
        count = len(backend._selection_units(focus, ctx.native.read_text(window.hwnd))) // 2
        ctx.wait(lambda: backend.selection(window) == (0, count), "Notepad select-all")
    return "Key events inserted; application consumption is not observable"


def type_text(ctx, action):
    window = ctx.target()
    backend = _backend(ctx)
    if getattr(window, "class_name", "") == "Notepad":
        def editor_ready():
            _foreground_guard(ctx, window)
            try:
                return backend._focus(window)
            except UnsupportedEditControl:
                return None
        ctx.wait(editor_ready, "Notepad's actual focused editor")
    text = action["text"]
    if "\0" in text:
        raise ExecutionError("NUL text cannot be typed safely")
    try:
        text.encode("utf-16-le", errors="strict")
    except UnicodeError as exc:
        raise ExecutionError("Text contains an invalid Unicode surrogate") from exc
    if len(text) >= int(ctx.settings.get("clipboard_threshold", 2000)):
        if backend.modifiers_held():
            raise ExecutionError("A physical modifier key is held; paste refused")
        _foreground_guard(ctx, window)
        backend.paste(ctx, window, text)
        detail = "Clipboard paste and resulting text confirmed; original clipboard preserved"
    else:
        if backend.modifiers_held():
            raise ExecutionError("A physical modifier key is held; text insertion refused")
        _foreground_guard(ctx, window)
        if backend.replace_text(ctx, window, text):
            detail = "Edit/RichEdit selection replacement and resulting text confirmed; undo preserved"
        else:
            # Keep each supplementary code point's surrogate pair in one batch.
            # There is no portable consumption acknowledgement for other frameworks.
            for char in text.replace("\r\n", "\n").replace("\r", "\n"):
                _send(ctx, window, unicode_events(char))
            detail = "UTF-16 text events inserted per code point; application consumption is not observable"
    if action["submit"]:
        _send(ctx, window, key_events("enter"))
    return detail


def scroll(ctx, action):
    direction = action["dir"]
    if direction in {"top", "bottom"}:
        return press(ctx, {"keys": "ctrl+home" if direction == "top" else "ctrl+end"})
    notches = {"small": 1, "page": 3, "large": 9}[action["amount"]]
    delta = 120 * notches * (1 if direction == "up" else -1)
    window = ctx.target()
    if not _backend(ctx).wheel_target(window):
        raise ExecutionError("Pointer is over another window; move it over the foreground app before scrolling")
    _send(ctx, window, [(0, 0, delta, 0x0800)])
    return f"Wheel input inserted ({notches} notches {direction})"


def volume(ctx, action):
    window, backend, op = ctx.target(), _backend(ctx), action["op"]
    _foreground_guard(ctx, window)
    muted, level = backend.audio_state()
    if op in {"mute", "unmute"}:
        desired = op == "mute"
        if muted == desired:
            return f"Default audio endpoint already {'muted' if desired else 'unmuted'}"
        _send(ctx, window, [key_event(0xAD), key_event(0xAD, True)])
        ctx.wait(lambda: backend.audio_state()[0] == desired, "default endpoint mute state")
        return f"Default audio endpoint {'muted' if desired else 'unmuted'} confirmed"
    vk = 0xAF if op == "up" else 0xAE
    _send(ctx, window, [key_event(vk), key_event(vk, True)])
    ctx.wait(lambda: (level >= 0.999 or backend.audio_state()[1] > level if op == "up"
                      else level <= 0.001 or backend.audio_state()[1] < level),
             "default endpoint volume direction")
    return f"Default audio endpoint volume {op} confirmed (or already at limit)"


def media(ctx, action):
    vk = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1}[action["op"]]
    _send(ctx, ctx.target(), [key_event(vk), key_event(vk, True)])
    return "Media key inserted; player state is not observable"
