"""Exact Copilot chord capture. No keys or transcript text are retained.

Windows reserves this chord from RegisterHotKey on some machines. A tiny
WH_KEYBOARD_LL callback on the existing hotkey pump handles only physical
Left-Shift + Windows + F23. Every other event passes through unchanged.
"""

import ctypes as C

from ..executor.native import BOOL, DWORD, HANDLE, LPARAM, _bind


class CopilotChord:
    def __init__(self, emit):
        self.emit = emit
        self.modifiers = set()
        self.pressed = False

    def key(self, vk, down, *, injected=False):
        if injected:
            return False
        if vk in {0xA0, 0x5B, 0x5C}:  # left shift, either Windows key
            if down:
                self.modifiers.add(vk)
            else:
                self.modifiers.discard(vk)
            return False
        if vk != 0x86:  # VK_F23
            return False
        if down and (self.pressed or
                     (0xA0 in self.modifiers and self.modifiers & {0x5B, 0x5C})):
            if not self.pressed:
                self.pressed = True
                self.emit()
            return True
        if not down and self.pressed:
            self.pressed = False
            return True
        return False


class CopilotHook:
    def __init__(self, events, *, user32=None):
        self.user = user32 or C.WinDLL("user32", use_last_error=True)
        self.handle = None
        self.chord = CopilotChord(lambda: events.put("toggle_listening"))
        self.callback_type = C.WINFUNCTYPE(LPARAM, C.c_int, C.c_size_t, LPARAM)
        _bind(self.user, "SetWindowsHookExW", HANDLE, C.c_int, self.callback_type, HANDLE, DWORD)
        _bind(self.user, "CallNextHookEx", LPARAM, HANDLE, C.c_int, C.c_size_t, LPARAM)
        _bind(self.user, "UnhookWindowsHookEx", BOOL, HANDLE)

        class KeyboardEvent(C.Structure):
            _fields_ = [("vk", DWORD), ("scan", DWORD), ("flags", DWORD),
                        ("time", DWORD), ("extra", C.c_size_t)]

        def receive(code, message, address):
            if code == 0 and message in {0x100, 0x101, 0x104, 0x105}:
                event = C.cast(address, C.POINTER(KeyboardEvent)).contents
                if self.chord.key(event.vk, message in {0x100, 0x104},
                                  injected=bool(event.flags & 0x12)):
                    return 1
            return self.user.CallNextHookEx(self.handle, code, message, address)

        self.callback = self.callback_type(receive)

    def register(self):
        self.handle = self.user.SetWindowsHookExW(13, self.callback, None, 0)
        if not self.handle:
            raise OSError(C.get_last_error(), "Copilot key capture could not start; use Ctrl+Alt+F9")

    def close(self):
        if self.handle:
            if not self.user.UnhookWindowsHookEx(self.handle):
                raise C.WinError(C.get_last_error())
            self.handle = None
