"""Exact, bounded Windows UI Automation on one windowless MTA worker.

COM elements never cross the worker's boundary. A timed-out request is revoked
before any later mutation, including a provider that finishes enumeration late.
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time

from .common import ExecutionError

_IMPORT_LOCK = threading.Lock()
_UNSAFE = threading.Event()
MAX_CONTROLS = 5000
MAX_BROWSER_ANCESTORS = 64


class ControlTreeChanged(ExecutionError):
    """A provider invalidated an element while its tree was being read."""


class ControlMatchError(ExecutionError):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


def _same(value):
    return " ".join(value.split()).casefold()


def matches(row, selector):
    return all(_same(str(row.get(key, ""))) == _same(value)
               for key, value in selector.items() if key != "timeout")


def unique(rows, selector):
    found = [row for row in rows if matches(row, selector)]
    if len(found) != 1:
        candidates = found or [row for row in rows if row.get("clickable")] or rows
        raise ControlMatchError(
            f"Expected one control matching {selector}; found {len(found)}. No control was clicked.",
            [{key: row[key] for key in ("name", "role", "automation_id", "enabled")}
             for row in candidates[:30]])
    return found[0]


class Automation:
    def __init__(self, *, timeout=8.0):
        if _UNSAFE.is_set():
            raise ExecutionError("A UI Automation worker did not finish; restart Voicebar before further UI Automation")
        self.timeout = timeout
        self.requests = queue.Queue()
        self.closed = False
        self.thread = threading.Thread(target=self._worker, name="voicebar-uia", daemon=True)
        self.thread.start()

    def _request(self, kind, *args):
        if self.closed:
            raise ExecutionError("UI Automation has closed")
        reply, revoked = queue.Queue(maxsize=1), threading.Event()
        self.requests.put((kind, args, reply, revoked))
        try:
            ok, value = reply.get(timeout=self.timeout)
        except queue.Empty as error:
            revoked.set()
            self.closed = True
            _UNSAFE.set()
            raise ExecutionError("UI Automation timed out; restart Voicebar before further UI Automation") from error
        if not ok:
            raise value
        return value

    def snapshot(self, hwnd):
        return self._request("snapshot", hwnd)

    def under_pointer(self):
        return self._request("pointer")

    def focused(self):
        """Read the focused element on the COM worker for state verification."""
        return self._request("focused")

    def click(self, hwnd, selector, guard):
        return self._request("click", hwnd, selector, guard)

    def find(self, hwnd, selector):
        """Read exact matches without collecting unrelated pattern properties."""
        return self._request("find", hwnd, selector)

    def browser_tabs(self, hwnd):
        """Read tab-strip items, excluding all descendants of web documents."""
        return self._request("browser_tabs", hwnd)

    def close(self):
        self.closed = True
        self.requests.put(None)
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            _UNSAFE.set()

    @staticmethod
    def _library():
        # comtypes initializes its importing thread. Balance that automatic
        # initialization before explicitly entering our MTA. No UI thread owns
        # these objects, and subsequent workers initialize independently.
        with _IMPORT_LOCK:
            first = "comtypes" not in sys.modules
            import comtypes
            if first:
                comtypes.CoUninitialize()
            comtypes.CoInitializeEx(0)
            import comtypes.client
            comtypes.client.gen_dir = None  # in-memory type bindings, no user cache writes
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            client = comtypes.client.CreateObject(module.CUIAutomation8,
                                                 interface=module.IUIAutomation2)
            client.ConnectionTimeout = 1200
            client.TransactionTimeout = 1200
        return comtypes, module, client

    def _worker(self):
        library = client = module = None
        failure = None
        try:
            try:
                library, module, client = self._library()
            except Exception as error:  # noqa: BLE001 - transfer native initialization failures
                failure = ExecutionError(f"Cannot initialize Windows UI Automation: {error}")
            while True:
                request = self.requests.get()
                if request is None:
                    break
                kind, args, reply, revoked = request
                try:
                    if failure is not None:
                        raise failure

                    def check(revoked=revoked):
                        if revoked.is_set():
                            raise ExecutionError("UI Automation request revoked")
                    if kind == "focused":
                        check()
                        element = client.GetFocusedElement()
                        value = self._details(element, module, 0)
                        value["has_keyboard_focus"] = bool(element.CurrentHasKeyboardFocus)
                    elif kind == "pointer":
                        from ctypes import wintypes
                        point = wintypes.POINT()
                        get = ctypes.WinDLL("user32", use_last_error=True).GetCursorPos
                        get.argtypes, get.restype = [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL
                        if not get(ctypes.byref(point)):
                            raise ExecutionError("Cannot read the pointer position")
                        element = client.ElementFromPoint(module.tagPOINT(point.x, point.y))
                        value = self._details(element, module, 0)
                    else:
                        check()
                        root = client.ElementFromHandle(args[0])
                        if kind == "snapshot":
                            rows, elements = self._tree(root, client, module, check)
                            value = rows
                        elif kind == "browser_tabs":
                            value = self._browser_tab_controls(root, client, module, check)
                        else:
                            selector = args[1]
                            rows, elements = self._controls(root, client, module, selector, check)
                            if kind == "find":
                                reply.put((True, [row for row in rows if matches(row, selector)]))
                                continue
                            guard = args[2]
                            row = unique(rows, selector)
                            element = elements[rows.index(row)]
                            row = self._details(element, module, 0)
                            if not matches(row, selector):
                                raise ControlTreeChanged("Matched control changed before its action")
                            if not row["enabled"] or not row["clickable"]:
                                raise ControlMatchError("The matched control is disabled or not actionable", [row])
                            # Query the action pattern before the final foreground
                            # guard, so blocked provider queries cannot authorize
                            # a late click after focus/cancellation changes.
                            pattern, method = self._pattern(element, module, row)
                            check()
                            guard()
                            check()
                            getattr(pattern, method)()
                            value = {key: row[key] for key in ("name", "role", "automation_id")}
                    reply.put((True, value))
                except Exception as error:  # noqa: BLE001 - transfer provider failures to caller
                    # Windows SDK UIAutomationCoreApi.h: UIA_E_ELEMENTNOTAVAILABLE.
                    # Only state waits may retry this read; mutations never retry.
                    if getattr(error, "hresult", None) == -2147220991:
                        error = ControlTreeChanged("UI Automation tree changed while being read")
                    elif not isinstance(error, ExecutionError):
                        error = ExecutionError(f"UI Automation failed: {error}")
                    reply.put((False, error))
                finally:
                    # Release every element while its owning apartment lives.
                    root = element = pattern = None
                    rows = elements = None
        finally:
            client = module = None
            if library is not None:
                library.CoUninitialize()

    @staticmethod
    def _role(element, module):
        control_type = int(element.CurrentControlType)
        role = element.CurrentLocalizedControlType or ""
        # Prefer the nonlocalized UIA control-type spelling when available.
        for key, value in vars(module).items():
            if key.startswith("UIA_") and key.endswith("ControlTypeId") and value == control_type:
                role = key[4:-13].lower()
                break
        return role

    @classmethod
    def _details(cls, element, module, depth):
        role = cls._role(element, module)
        invoke = bool(element.GetCurrentPropertyValue(module.UIA_IsInvokePatternAvailablePropertyId))
        select = bool(element.GetCurrentPropertyValue(module.UIA_IsSelectionItemPatternAvailablePropertyId))
        focus = bool(element.CurrentIsKeyboardFocusable)
        return {"name": element.CurrentName or "", "role": role,
                "automation_id": element.CurrentAutomationId or "",
                "keyboard_shortcut": element.CurrentAcceleratorKey or element.CurrentAccessKey or "",
                "enabled": bool(element.CurrentIsEnabled), "depth": depth,
                "pid": int(element.CurrentProcessId),
                "hwnd": int(element.CurrentNativeWindowHandle or 0),
                "clickable": invoke or select or (focus and role in {"edit", "document"}),
                "invokable": invoke, "selectable": select, "focusable": focus}

    def _controls(self, root, client, module, selector, check):
        """Complete role-filtered ControlView query; normalize names in Python.

        Native name/id equality would lose our whitespace/case semantics. Read
        every candidate identity before selecting, so duplicate names still fail.
        Pattern properties are fetched only for the one selected element.
        """
        deadline = time.monotonic() + self.timeout - .25

        def bounded():
            check()
            if time.monotonic() >= deadline:
                raise ExecutionError("UI Automation query exceeded its time limit; no action ran")

        condition = client.ControlViewCondition
        roles = {key[4:-13].lower(): value for key, value in vars(module).items()
                 if key.startswith("UIA_") and key.endswith("ControlTypeId")}
        role_id = roles.get(_same(selector.get("role", "")))
        if role_id is not None:
            condition = client.CreateAndCondition(condition, client.CreatePropertyCondition(
                module.UIA_ControlTypePropertyId, role_id))
        bounded()
        found = root.FindAll(module.TreeScope_Subtree, condition)
        bounded()
        count = int(found.Length)
        if count > MAX_CONTROLS:
            raise ExecutionError("UI Automation query is too large; no action ran")
        rows, elements = [], []
        for index in range(count):
            bounded()
            element = found.GetElement(index)
            rows.append({"name": element.CurrentName or "", "role": self._role(element, module),
                         "automation_id": element.CurrentAutomationId or "",
                         "enabled": bool(element.CurrentIsEnabled)})
            elements.append(element)
        bounded()
        return rows, elements

    def _browser_tab_controls(self, root, client, module, check):
        """Prove each tab's ancestry reaches this HWND without a Document.

        UIA tabitem also describes ARIA tabs inside pages. The role alone is
        insufficient. Parent traversal and element comparison stay on this
        request's COM worker; an incomplete ancestry rejects the entire query.
        """
        deadline = time.monotonic() + self.timeout - .25

        def bounded():
            check()
            if time.monotonic() >= deadline:
                raise ExecutionError("Browser tab ancestry query exceeded its time limit; no action ran")

        rows, elements = self._controls(root, client, module, {"role": "tabitem"}, bounded)
        bounded()
        walker = client.ControlViewWalker
        accepted = []
        for row, element in zip(rows, elements, strict=True):
            if _same(row["role"]).replace(" ", "") != "tabitem":
                continue
            parent = element
            for _ in range(MAX_BROWSER_ANCESTORS):
                bounded()
                parent = walker.GetParentElement(parent)
                bounded()
                if not parent:
                    raise ControlTreeChanged("Browser tab ancestry no longer reaches its original window")
                if int(parent.CurrentControlType) == module.UIA_DocumentControlTypeId:
                    break
                bounded()
                same_root = client.CompareElements(parent, root)
                bounded()
                if same_root:
                    accepted.append(row)
                    break
            else:
                raise ExecutionError("Browser tab ancestry is too deep or cyclic; no action ran")
        bounded()
        return accepted

    def _tree(self, root, client, module, check):
        todo, rows, elements = [(root, 0)], [], []
        walker = client.ControlViewWalker
        deadline = time.monotonic() + self.timeout - .25
        while todo:
            check()
            if time.monotonic() >= deadline or len(rows) >= MAX_CONTROLS:
                raise ExecutionError("UI Automation tree exceeded its time or control limit; no action ran")
            element, depth = todo.pop()
            rows.append(self._details(element, module, depth))
            elements.append(element)
            children = []
            child = walker.GetFirstChildElement(element)
            while child:
                check()
                if len(children) + len(todo) + len(rows) >= MAX_CONTROLS:
                    raise ExecutionError("UI Automation tree is too large; no action ran")
                children.append((child, depth + 1))
                child = walker.GetNextSiblingElement(child)
            todo.extend(reversed(children))
        return rows, elements

    @staticmethod
    def _pattern(element, module, row):
        if row["invokable"]:
            return (element.GetCurrentPattern(module.UIA_InvokePatternId).QueryInterface(
                module.IUIAutomationInvokePattern), "Invoke")
        if row["selectable"]:
            return (element.GetCurrentPattern(module.UIA_SelectionItemPatternId).QueryInterface(
                module.IUIAutomationSelectionItemPattern), "Select")
        return element, "SetFocus"
