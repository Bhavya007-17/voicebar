"""Resolve installed app identities and operate only on matching top-level windows."""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
from pathlib import Path

from .common import ExecutionError

_APPS_FOLDER_COMMAND = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$folder = (New-Object -ComObject Shell.Application).NameSpace('shell:AppsFolder')
if ($null -eq $folder) { throw 'Windows AppsFolder is unavailable' }
$rows = @(foreach ($item in $folder.Items()) {
    [pscustomobject]@{
        Name = [string]$item.Name
        AppID = [string]$item.ExtendedProperty('System.AppUserModel.ID')
        Executable = [string]$item.ExtendedProperty('System.Link.TargetParsingPath')
        Arguments = [string]$item.ExtendedProperty('System.Link.Arguments')
    }
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
"""


def _enumerate_metadata():
    """Read shell metadata; never interpolate a transcript or invoke an app."""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _APPS_FOLDER_COMMAND],
            shell=False, capture_output=True, encoding="utf-8-sig", errors="strict",
            timeout=30, check=True,
        )
        return json.loads(result.stdout)
    except (OSError, UnicodeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ExecutionError(f"Cannot read Windows app identity metadata: {error}") from error


def _path(value):
    if not isinstance(value, str) or not ntpath.isabs(value):
        return None
    return ntpath.normcase(ntpath.normpath(value))


def _phrase(value):
    return "".join(value.lower().split())


def _metadata(rows):
    if not isinstance(rows, list):
        raise ExecutionError("App identity cache must contain a list of metadata records")
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str)
            for key in ("Name", "AppID", "Executable", "Arguments")
        ):
            raise ExecutionError("App identity metadata needs Name, AppID, Executable and Arguments text")
    return rows


class AppRegistry:
    """AUMIDs first, unique full executable paths second; never window titles."""

    def __init__(self, root, loaded, native, settings):
        self.root = Path(root)
        self.loaded = loaded
        self.native = native
        self.settings = settings
        self.cache = self.root / ".cache/app-identities.json"
        self._apps = list(loaded.installed_apps)
        self._ids = {app.app_id for app in self._apps}
        self.new_window_apps = self._configured_names("new_window_apps")
        self.browser_apps = self._configured_names("browser_apps")
        self.overrides = {}
        configured = settings.get("window_executables", {})
        if not isinstance(configured, dict):
            raise ExecutionError("executor window_executables must map app names to path lists")
        for name, paths in configured.items():
            app = self.app(name)
            if not isinstance(paths, list) or not paths:
                raise ExecutionError(f"window_executables for {name!r} needs a nonempty path list")
            verified = []
            for path in paths:
                normalized = _path(path)
                if normalized is None or not Path(path).is_file():
                    raise ExecutionError(f"Window executable for {name!r} does not exist: {path}")
                verified.append(normalized)
            self.overrides[app.app_id] = verified
        if self.cache.exists():
            try:
                self.rows = _metadata(json.loads(self.cache.read_text(encoding="utf-8-sig")))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ExecutionError(f"Cannot read app identity cache; refresh the app catalog: {error}") from error
            self._index()
        else:
            self.refresh()

    def app(self, name):
        if not isinstance(name, str):
            raise ExecutionError("App target must be an installed app name")
        exact = [app for app in self._apps if app.name == name]
        equivalents = [app for app in self._apps if _phrase(app.name) == _phrase(name)]
        if len(exact) != 1 or len(equivalents) != 1:
            raise ExecutionError(f"Installed app {name!r} is missing or ambiguous")
        return exact[0]

    def _configured_names(self, key):
        values = self.settings.get(key, [])
        if not isinstance(values, list):
            raise ExecutionError(f"executor {key} must be a list of installed app names")
        for name in values:
            self.app(name)
        if len(set(values)) != len(values):
            raise ExecutionError(f"executor {key} contains a duplicate app")
        return set(values)

    def refresh(self):
        rows = _metadata(_enumerate_metadata())
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.cache)
        except OSError as error:
            raise ExecutionError(f"Cannot save app identity cache: {error}") from error
        self.rows = rows
        self._index()

    def _index(self):
        self._paths = {}
        self._path_owners = {}
        for index, row in enumerate(self.rows):
            path = _path(row["Executable"])
            if path is None:
                continue
            # Unknown registrations still count as a possible shared-host owner.
            owner = row["AppID"] or f"unknown:{index}"
            self._path_owners.setdefault(path, set()).add(owner)
            if row["AppID"] in self._ids and not row["Arguments"].strip():
                self._paths.setdefault(row["AppID"], set()).add(path)
        for app_id, paths in self.overrides.items():
            for path in paths:
                self._path_owners.setdefault(path, set()).add(app_id)
                self._paths.setdefault(app_id, set()).add(path)

    def matches(self, window, app_name):
        app = self.app(app_name)
        known_ids = set(window.aumids) & self._ids
        if known_ids:
            # Conflicting host/child IDs are ambiguity, not permission to guess.
            return known_ids == {app.app_id}
        executable = _path(window.exe)
        return (
            executable is not None
            and executable in self._paths.get(app.app_id, set())
            and self._path_owners.get(executable) == {app.app_id}
        )

    def name_for_window(self, window):
        """Resolve one observed identity without probing unrelated app names.

        Duplicate Start-menu display names remain unaddressable, but must not
        prevent recognizing an unrelated, uniquely named foreground app.
        """
        identities = set(window.aumids) & self._ids
        if not identities:
            executable = _path(window.exe)
            owners = self._path_owners.get(executable, set())
            if len(owners) != 1:
                return None
            identities = owners & self._ids
            if not identities or executable not in self._paths.get(next(iter(identities)), set()):
                return None
        if len(identities) != 1:
            return None
        candidates = [app for app in self._apps if app.app_id in identities]
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        try:
            self.app(candidate.name)
        except ExecutionError:
            return None
        return candidate.name

    def find(self, app_name, owned=None):
        self.app(app_name)
        return [
            window for window in self.native.windows()
            if (owned is None or owned.get(window.hwnd) == window.pid)
            and self.matches(window, app_name)
        ]

    def browser(self, window):
        return any(self.matches(window, name) for name in self.browser_apps)


def _choose(ctx, windows):
    foreground = ctx.native.foreground()
    # EnumWindows order is topmost first; multiple genuine windows are valid.
    return next((window for window in windows if window.hwnd == foreground), windows[0])


def _activate(ctx, window):
    ctx.guard(window)
    ctx.begin()
    ctx.native.activate(window.hwnd, ctx.timeout)
    ctx.guard(window)
    if ctx.native.foreground() != window.hwnd:
        raise ExecutionError("Windows did not grant foreground activation")


def _remember(ctx, app_name):
    ctx.last_app = app_name
    ctx.save()


def open_app(ctx, action):
    name = action["app"]
    app = ctx.apps.app(name)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    existing = ctx.apps.find(name)
    eligible = [window for window in existing if ctx.owned is None or ctx.owned.get(window.hwnd) == window.pid]
    needs_new = name in ctx.apps.new_window_apps
    if existing and not needs_new:
        if not eligible:
            raise ExecutionError(f"Test refused to activate an unowned {name} window")
        _activate(ctx, _choose(ctx, eligible))
        _remember(ctx, name)
        return f"Activated {name}"

    shortcut = None
    current = _choose(ctx, eligible) if eligible else None
    if needs_new and current is not None:
        if name == "Notepad":
            shortcut = "ctrl+shift+n"
        elif name == "File Explorer" or ctx.apps.browser(current):
            shortcut = "ctrl+n"
    if shortcut is not None:
        from .input import press_window
        _activate(ctx, current)
        press_window(ctx, current, shortcut)
    else:
        # AppsFolder activation may reuse a user's existing window. A test may not
        # even trigger that activation; decline before the first mutation.
        if ctx.owned is not None and any(ctx.owned.get(window.hwnd) != window.pid for window in existing):
            raise ExecutionError(f"Test refused to launch {name} while unowned matching windows exist")
        ctx.begin()
        ctx.native.launch_app(app.app_id)

    def new_window():
        candidates = [
            window for window in ctx.apps.find(name)
            if (window.hwnd, window.pid) not in baseline
        ]
        if len(candidates) > 1:
            raise ExecutionError(f"Multiple new {name} windows appeared; ownership is ambiguous")
        return _choose(ctx, candidates) if candidates else None

    try:
        window = ctx.wait(new_window, f"a new {name} window")
    except RuntimeError as error:
        raise ExecutionError(f"No new {name} window was confirmed; existing windows were not adopted: {error}") from error
    ctx.adopt(window)
    _activate(ctx, window)
    _remember(ctx, name)
    return f"Opened new {name} window"


def switch_app(ctx, action):
    name = action["app"]
    windows = ctx.apps.find(name, ctx.owned)
    if not windows:
        raise ExecutionError(f"No eligible open window for {name}")
    _activate(ctx, _choose(ctx, windows))
    _remember(ctx, name)
    return f"Switched to {name}"


def close_app(ctx, action):
    name = action["app"]
    windows = ctx.apps.find(name, ctx.owned)
    if not windows:
        raise ExecutionError(f"No eligible open window for {name}")
    # Validate all known targets before any close; revalidate at each mutation.
    for window in windows:
        ctx.guard(window)
    for window in windows:
        ctx.guard(window)
        ctx.begin()
        ctx.native.close(window.hwnd)
    try:
        ctx.wait(
            lambda: all(not ctx.native.is_window(window.hwnd, window.pid) for window in windows),
            f"{name} windows to close",
        )
    except RuntimeError as error:
        raise ExecutionError(f"{name} remains open; save or discard changes manually. No process was terminated: {error}") from error
    if ctx.last_app == name:
        ctx.last_app = None
        ctx.save()
    return f"Closed {len(windows)} {name} window(s)"
