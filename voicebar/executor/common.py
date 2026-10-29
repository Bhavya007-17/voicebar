"""Execution context, configuration and ownership boundaries."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from time import perf_counter

from ..catalog import CatalogError, _yaml_file, load_catalog


class ExecutionError(RuntimeError):
    """A command could not be performed safely or confirmed."""


def load_settings(root, loaded, document=None):
    document = _yaml_file(root / "config/settings.yaml") if document is None else document
    settings = document.get("executor", {})
    if not isinstance(settings, dict):
        raise CatalogError("settings.yaml executor must be a mapping")
    allowed = {"window_timeout_seconds", "new_window_apps", "browser_apps",
               "window_executables", "clipboard_threshold"}
    unknown = set(settings) - allowed
    if unknown:
        raise CatalogError(f"settings.yaml executor: unknown settings {sorted(unknown)}")
    result = {"window_timeout_seconds": 30, "new_window_apps": [], "browser_apps": [],
              "window_executables": {}, "clipboard_threshold": 256, **settings}
    timeout = result["window_timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise CatalogError("executor window_timeout_seconds must be a number from 0 to 120")
    threshold = result["clipboard_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 100000:
        raise CatalogError("executor clipboard_threshold must be an integer from 1 to 100000")
    for key in ("new_window_apps", "browser_apps"):
        entries = result[key]
        if not isinstance(entries, list) or any(not isinstance(name, str) for name in entries):
            raise CatalogError(f"executor {key} must be a list of installed app names")
        for name in entries:
            if loaded.catalog.apps.count(name) != 1:
                raise CatalogError(f"executor {key}: app '{name}' is missing or ambiguous")
        if len(entries) != len(set(entries)):
            raise CatalogError(f"executor {key} contains a duplicate app")
    overrides = result["window_executables"]
    if not isinstance(overrides, dict):
        raise CatalogError("executor window_executables must map app names to executable paths")
    for name, paths in overrides.items():
        if loaded.catalog.apps.count(name) != 1:
            raise CatalogError(f"executor window_executables: app '{name}' is missing or ambiguous")
        if not isinstance(paths, list) or not paths:
            raise CatalogError(f"executor window_executables '{name}' needs a nonempty path list")
        for path in paths:
            if not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file():
                raise CatalogError(f"executor window_executables '{name}': file does not exist: {path}")
    return result


def state_path(root):
    """An explicit test session only restricts access; it never expands privileges."""
    session = os.environ.get("VOICEBAR_TEST_SESSION")
    return Path(session) if session else root / ".cache/runtime.json"


def read_state(root):
    path = state_path(root)
    if not path.exists():
        if os.environ.get("VOICEBAR_TEST_SESSION"):
            raise ExecutionError("Test ownership session does not exist; refusing to act")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("expected object")
        if os.environ.get("VOICEBAR_TEST_SESSION") and not isinstance(data.get("owned"), dict):
            raise ValueError("test session needs owned HWND/PID mapping")
        return data
    except (OSError, ValueError, TypeError) as error:
        raise ExecutionError(f"Cannot read execution state: {error}") from error


class Context:
    def __init__(self, root=None, *, loaded=None, native=None, apps=None, owned=None,
                 settings=None, persist=True, cancel=None, monitor_reference="window",
                 remember_app=True):
        if monitor_reference not in {"window", "cursor"}:
            raise ValueError("Unknown monitor reference")
        self.monitor_reference = monitor_reference
        self.cancel = cancel
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.loaded = loaded if loaded is not None else load_catalog(self.root)
        self.settings = settings if settings is not None else load_settings(self.root, self.loaded)
        if native is None:
            from .native import NativeWindows
            native = NativeWindows()
        self.native = native
        self.timeout = self.settings.get("window_timeout_seconds", 30)
        self.persist = persist
        self.remember_app = remember_app
        state = read_state(self.root) if persist and (
            remember_app or os.environ.get("VOICEBAR_TEST_SESSION")) else {}
        if os.environ.get("VOICEBAR_TEST_SESSION") and persist:
            self.owned = {int(hwnd): int(pid) for hwnd, pid in state["owned"].items()}
        else:
            self.owned = dict(owned) if owned is not None else None
        self.last_app = state.get("last_app") if remember_app else None
        self.ownership_record = (self.root / f".cache/test-ownership-{os.getpid()}-{uuid.uuid4().hex}.json"
                                 if self.owned is not None and not persist else None)
        self.started_at = None
        self.entered_at = None
        if apps is None:
            from .apps import AppRegistry
            apps = AppRegistry(self.root, self.loaded, self.native, self.settings)
        self.apps = apps

    def close(self):
        automation = getattr(self, "automation", None)
        if automation is not None:
            automation.close()
        input_backend = getattr(self, "input_backend", None)
        if input_backend is not None and getattr(input_backend, "close", None) is not None:
            input_backend.close()
        shutdown = getattr(self.native, "shutdown", None)
        if shutdown is not None:
            shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def begin(self):
        self.check_cancelled()
        if self.started_at is None:
            self.started_at = perf_counter()

    def check_cancelled(self):
        cancel = getattr(self, "cancel", None)
        if cancel is not None and cancel.is_set():
            raise ExecutionError("Action cancelled or timed out; further effects refused")

    def guard(self, window):
        self.check_cancelled()
        expected = getattr(self, "required_foreground", None)
        if expected is not None and (
                (window.hwnd, window.pid) != expected or self.native.foreground() != expected[0]):
            raise ExecutionError("The observed target window changed; repeat the command")
        if not self.native.is_window(window.hwnd, window.pid):
            raise ExecutionError("The target window closed or its identity changed")
        if self.owned is not None and self.owned.get(window.hwnd) != window.pid:
            raise ExecutionError("Test refused to touch a window it did not open")
        try:
            if self.native.integrity(window.pid) > self.native.current_integrity():
                raise ExecutionError("The target is elevated; a normal process cannot send it input or window messages")
        except OSError as error:
            raise ExecutionError(f"Cannot check target integrity: {error}") from error
        return window

    def adopt(self, window):
        if not self.native.is_window(window.hwnd, window.pid):
            raise ExecutionError("New window disappeared before it could be recorded")
        if self.owned is not None:
            self.owned[window.hwnd] = window.pid
        self.save()

    def wait(self, predicate, description):
        def checked():
            self.check_cancelled()
            return predicate()
        self.check_cancelled()
        return self.native.wait(checked, self.timeout, description)

    def target(self, target="active"):
        if target == "active":
            hwnd = self.native.foreground()
            matches = [window for window in self.native.windows() if window.hwnd == hwnd]
            if not matches:
                raise ExecutionError("No eligible foreground window")
            return self.guard(matches[0])
        matches = self.apps.find(target, self.owned)
        if not matches:
            matches = self.wait(lambda: self.apps.find(target, self.owned),
                                f"a window for {target}")
        # Prefer foreground; otherwise EnumWindows gives topmost eligible main window.
        hwnd = self.native.foreground()
        return self.guard(next((window for window in matches if window.hwnd == hwnd), matches[0]))

    def save(self):
        if not self.persist and self.ownership_record is None:
            return
        if not self.remember_app and self.owned is None:
            return
        path = state_path(self.root) if self.persist else self.ownership_record
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"last_app": self.last_app} if self.remember_app else {}
        if self.owned is not None:
            state["owned"] = {str(hwnd): pid for hwnd, pid in self.owned.items()}
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
