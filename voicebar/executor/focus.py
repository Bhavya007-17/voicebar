"""UI Automation focus queries on a separate windowless MTA thread."""

import ctypes as C
from queue import Empty, Queue
from threading import Thread
from uuid import UUID

from .common import ExecutionError


class FocusReader:
    def __init__(self):
        self.requests = Queue()
        self.thread = Thread(target=self._worker, name="voicebar-uia-focus", daemon=True)
        self.thread.start()

    def focused(self):
        reply = Queue(maxsize=1)
        self.requests.put(reply)
        try:
            value = reply.get(timeout=3)
        except Empty as error:
            raise ExecutionError("UI Automation focus query timed out; input refused") from error
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.requests.put(None)
        self.thread.join(timeout=3)

    def _worker(self):
        ole = C.WinDLL("ole32", use_last_error=True)
        ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_uint32]
        ole.CoInitializeEx.restype = C.c_int32
        ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
        ole.CoCreateInstance.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32,
                                        C.c_void_p, C.POINTER(C.c_void_p)]
        ole.CoCreateInstance.restype = C.c_int32
        initialized = ole.CoInitializeEx(None, 0)
        automation = C.c_void_p()
        failure = None

        def check(hr):
            if hr < 0:
                raise ExecutionError(f"UI Automation focus query failed (0x{hr & 0xffffffff:08x})")

        def call(pointer, slot, *types):
            table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            return C.WINFUNCTYPE(C.c_int32, C.c_void_p, *types)(table[slot])

        try:
            try:
                check(initialized)
                clsid = C.create_string_buffer(UUID("ff48dba4-60ef-4201-aa87-54103eef594e").bytes_le, 16)
                iid = C.create_string_buffer(UUID("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee").bytes_le, 16)
                check(ole.CoCreateInstance(clsid, None, 1, iid, C.byref(automation)))
            except (ExecutionError, OSError, ValueError) as error:
                failure = error
            while True:
                reply = self.requests.get()
                if reply is None:
                    break
                if failure is not None:
                    reply.put(failure)
                    continue
                element = C.c_void_p()
                try:
                    check(call(automation, 8, C.POINTER(C.c_void_p))(automation, C.byref(element)))
                    if not element:
                        reply.put(None)
                        continue
                    hwnd, pid, focused, password = C.c_void_p(), C.c_int32(), C.c_int32(), C.c_int32()
                    check(call(element, 36, C.POINTER(C.c_void_p))(element, C.byref(hwnd)))
                    check(call(element, 20, C.POINTER(C.c_int32))(element, C.byref(pid)))
                    check(call(element, 26, C.POINTER(C.c_int32))(element, C.byref(focused)))
                    check(call(element, 35, C.POINTER(C.c_int32))(element, C.byref(password)))
                    reply.put((int(hwnd.value or 0), pid.value, bool(focused.value), bool(password.value)))
                except (ExecutionError, OSError, ValueError) as error:
                    reply.put(error)
                finally:
                    if element:
                        call(element, 2)(element)
        finally:
            if automation:
                call(automation, 2)(automation)
            if initialized >= 0:
                ole.CoUninitialize()
