"""App identity, launch ownership and close behavior using native-boundary fakes."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from voicebar.catalog import Catalog, InstalledApp, LoadedCatalog
from voicebar.executor import apps
from voicebar.executor.common import ExecutionError


def window(hwnd, *, pid=None, exe=r"C:\Apps\note.exe", aumids=(), title="Untitled"):
    return SimpleNamespace(hwnd=hwnd, pid=pid or hwnd + 100, tid=1, title=title,
                           class_name="Main", exe=exe, aumids=tuple(aumids))


class Native:
    def __init__(self, windows=()):
        self.items = list(windows)
        self.active = self.items[0].hwnd if self.items else 0
        self.calls = []
        self.on_launch = lambda: None
        self.keep_open = set()

    def windows(self):
        return list(self.items)

    def foreground(self):
        return self.active

    def is_window(self, hwnd, pid):
        return any(item.hwnd == hwnd and item.pid == pid for item in self.items)

    def activate(self, hwnd, timeout):
        self.calls.append(("activate", hwnd, timeout))
        self.active = hwnd

    def launch_app(self, app_id):
        self.calls.append(("launch", app_id))
        self.on_launch()

    def close(self, hwnd):
        self.calls.append(("close", hwnd))
        if hwnd not in self.keep_open:
            self.items = [item for item in self.items if item.hwnd != hwnd]

    def wait(self, predicate, timeout, description):
        result = predicate()
        if result:
            return result
        raise RuntimeError(f"Timed out waiting for {description}")


class Context:
    def __init__(self, registry, *, owned=None):
        self.apps = registry
        self.native = registry.native
        self.owned = owned
        self.timeout = 30
        self.last_app = None
        self.events = []
        self.blocked_pids = set()

    def guard(self, item):
        self.events.append(("guard", item.hwnd))
        if not self.native.is_window(item.hwnd, item.pid):
            raise ExecutionError("Window changed")
        if self.owned is not None and self.owned.get(item.hwnd) != item.pid:
            raise ExecutionError("Unowned window")
        if item.pid in self.blocked_pids:
            raise ExecutionError("Elevated window")
        return item

    def begin(self):
        self.events.append(("begin", len(self.native.calls)))

    def adopt(self, item):
        self.events.append(("adopt", item.hwnd, item.pid))
        if self.owned is not None:
            self.owned[item.hwnd] = item.pid

    def save(self):
        self.events.append(("save", self.last_app))

    def wait(self, predicate, description):
        return self.native.wait(predicate, self.timeout, description)


@pytest.fixture
def loaded():
    installed = [InstalledApp("Notepad", "Note.App"), InstalledApp("Calculator", "Calc.App"),
                 InstalledApp("Browser", "Browser.App")]
    return LoadedCatalog(Catalog(apps=[app.name for app in installed]), installed, {}, [])


@pytest.fixture
def rows():
    return [
        {"Name": "Notepad", "AppID": "Note.App", "Executable": r"C:\Apps\note.exe", "Arguments": ""},
        {"Name": "Calculator", "AppID": "Calc.App", "Executable": "", "Arguments": ""},
        {"Name": "Browser", "AppID": "Browser.App", "Executable": r"C:\Apps\browser.exe", "Arguments": ""},
    ]


def registry(tmp_path, loaded, rows, *, native=None, settings=None):
    cache = tmp_path / ".cache/app-identities.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    return apps.AppRegistry(tmp_path, loaded, native or Native(), settings or {})


def test_aumid_resolves_hosted_app_without_title_or_host_path_guess(tmp_path, loaded, rows):
    result = registry(tmp_path, loaded, rows)
    calculator = window(1, exe=r"C:\Windows\System32\ApplicationFrameHost.exe", aumids=["Calc.App"], title="Anything")
    assert result.matches(calculator, "Calculator")
    assert not result.matches(calculator, "Notepad")
    assert not result.matches(window(2, exe=calculator.exe, title="Calculator"), "Calculator")


def test_full_path_fallback_is_case_insensitive_but_never_basename_only(tmp_path, loaded, rows):
    result = registry(tmp_path, loaded, rows)
    assert result.matches(window(1, exe=r"c:\APPS\NOTE.exe"), "Notepad")
    assert not result.matches(window(2, exe=r"D:\Other\note.exe", title="Notepad"), "Notepad")


def test_known_other_or_conflicting_aumids_block_path_fallback(tmp_path, loaded, rows):
    result = registry(tmp_path, loaded, rows)
    assert not result.matches(window(1, aumids=["Calc.App"]), "Notepad")
    assert not result.matches(window(2, aumids=["Note.App", "Calc.App"]), "Notepad")
    assert not result.matches(window(2, aumids=["Note.App", "Calc.App"]), "Calculator")


def test_profile_specific_unknown_aumid_may_use_unique_full_path(tmp_path, loaded, rows):
    result = registry(tmp_path, loaded, rows)
    assert result.matches(window(1, aumids=["Note.App.Profile"]), "Notepad")


def test_shared_host_or_shortcut_arguments_fail_closed(tmp_path, loaded, rows):
    rows.append({"Name": "Unknown hosted tool", "AppID": "other.hosted", "Executable": r"C:\Apps\note.exe", "Arguments": "--tool"})
    result = registry(tmp_path, loaded, rows)
    assert not result.matches(window(1), "Notepad")
    assert result.matches(window(2, aumids=["Note.App"]), "Notepad")
    rows.pop()
    rows[0]["Arguments"] = "--different-experience"
    result = registry(tmp_path, loaded, rows)
    assert not result.matches(window(3), "Notepad")


def test_duplicate_display_name_cannot_be_selected(tmp_path, loaded, rows):
    loaded.installed_apps.append(InstalledApp("Notepad", "Second.Note"))
    result = registry(tmp_path, loaded, rows)
    with pytest.raises(ExecutionError, match="missing or ambiguous"):
        result.find("Notepad")


def test_browser_and_find_use_identity_and_owned_hwnd_pid(tmp_path, loaded, rows):
    first = window(1, exe=r"C:\Apps\browser.exe")
    second = window(2, exe=r"C:\Apps\browser.exe")
    result = registry(tmp_path, loaded, rows, native=Native([first, second]), settings={"browser_apps": ["Browser"]})
    assert result.browser(first)
    assert not result.browser(window(3, title="Browser"))
    assert result.find("Browser", {first.hwnd: first.pid}) == [first]
    assert result.find("Browser", {first.hwnd: first.pid + 1}) == []


def test_verified_override_handles_launcher_handoff(tmp_path, loaded, rows):
    executable = tmp_path / "RealNote.exe"
    executable.write_bytes(b"fixture")
    result = registry(tmp_path, loaded, rows, settings={"window_executables": {"Notepad": [str(executable)]}})
    assert result.matches(window(1, exe=str(executable)), "Notepad")


@pytest.mark.parametrize("settings", [
    {"new_window_apps": ["Unknown"]}, {"browser_apps": "Browser"},
    {"browser_apps": ["Browser", "Browser"]}, {"window_executables": []},
    {"window_executables": {"Notepad": "C:\\Apps\\note.exe"}},
    {"window_executables": {"Notepad": ["missing.exe"]}},
])
def test_invalid_registry_settings_fail_before_mutations(tmp_path, loaded, rows, settings):
    native = Native()
    with pytest.raises(ExecutionError):
        registry(tmp_path, loaded, rows, native=native, settings=settings)
    assert native.calls == []


def test_cache_avoids_repeated_metadata_subprocess_and_refresh_rebuilds(tmp_path, loaded, rows, monkeypatch):
    result = registry(tmp_path, loaded, rows)
    calls = []
    def metadata():
        calls.append(True)
        return rows
    monkeypatch.setattr(apps, "_enumerate_metadata", metadata)
    apps.AppRegistry(tmp_path, loaded, Native(), {})
    assert calls == []
    result.refresh()
    assert calls == [True]
    assert json.loads(result.cache.read_text(encoding="utf-8")) == rows


def test_missing_metadata_cache_enumerates_once(tmp_path, loaded, rows, monkeypatch):
    calls = []
    monkeypatch.setattr(apps, "_enumerate_metadata", lambda: calls.append(True) or rows)
    apps.AppRegistry(tmp_path, loaded, Native(), {})
    apps.AppRegistry(tmp_path, loaded, Native(), {})
    assert calls == [True]


def test_refresh_failure_does_not_replace_last_good_metadata(tmp_path, loaded, rows, monkeypatch):
    result = registry(tmp_path, loaded, rows)
    before = result.cache.read_bytes()
    monkeypatch.setattr(apps, "_enumerate_metadata", lambda: None)
    with pytest.raises(ExecutionError):
        result.refresh()
    assert result.cache.read_bytes() == before


def test_metadata_provider_is_fixed_read_only_powershell(monkeypatch, rows):
    seen = {}
    def run(arguments, **kwargs):
        seen.update(arguments=arguments, **kwargs)
        return SimpleNamespace(stdout=json.dumps(rows))
    monkeypatch.setattr(apps.subprocess, "run", run)
    assert apps._enumerate_metadata() == rows
    assert seen["shell"] is False
    assert seen["encoding"] == "utf-8-sig"
    assert seen["check"] is True
    assert seen["arguments"][-1] == apps._APPS_FOLDER_COMMAND
    assert "shell:AppsFolder" in seen["arguments"][-1]
    assert "launch" not in seen["arguments"][-1].lower()


@pytest.mark.parametrize("error", [FileNotFoundError("PowerShell"), subprocess.TimeoutExpired("PowerShell", 30)])
def test_metadata_provider_failures_are_plain_errors(monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(apps.subprocess, "run", run)
    with pytest.raises(ExecutionError, match="identity metadata"):
        apps._enumerate_metadata()


def test_open_ordinary_existing_app_activates_and_records_start(tmp_path, loaded, rows):
    existing = window(1)
    ctx = Context(registry(tmp_path, loaded, rows, native=Native([existing])))
    assert apps.open_app(ctx, {"app": "Notepad"}) == "Activated Notepad"
    assert ctx.native.calls == [("activate", existing.hwnd, 30)]
    assert ("begin", 0) in ctx.events
    assert ctx.last_app == "Notepad"


def test_switch_prefers_foreground_then_topmost_matching_window(tmp_path, loaded, rows):
    first, second = window(1), window(2)
    ctx = Context(registry(tmp_path, loaded, rows, native=Native([first, second])))
    ctx.native.active = second.hwnd
    apps.switch_app(ctx, {"app": "Notepad"})
    assert ctx.native.calls[-1][1] == second.hwnd
    ctx.native.active = 999
    apps.switch_app(ctx, {"app": "Notepad"})
    assert ctx.native.calls[-1][1] == first.hwnd


@pytest.mark.parametrize("handler", [apps.open_app, apps.switch_app, apps.close_app])
def test_unowned_existing_window_is_never_mutated(tmp_path, loaded, rows, handler):
    existing = window(1)
    ctx = Context(registry(tmp_path, loaded, rows, native=Native([existing])), owned={})
    with pytest.raises(ExecutionError):
        handler(ctx, {"app": "Notepad"})
    assert ctx.native.calls == []
    assert not any(event[0] == "begin" for event in ctx.events)


def test_new_window_launch_adopts_only_new_identity_match(tmp_path, loaded, rows):
    unrelated = window(1, exe=r"C:\Unrelated.exe")
    created = window(2, aumids=["Calc.App"])
    native = Native([unrelated])
    native.on_launch = lambda: native.items.append(created)
    ctx = Context(registry(tmp_path, loaded, rows, native=native), owned={})
    apps.open_app(ctx, {"app": "Calculator"})
    assert native.calls[0] == ("launch", "Calc.App")
    assert ctx.owned == {created.hwnd: created.pid}
    assert ("adopt", created.hwnd, created.pid) in ctx.events
    assert native.active == created.hwnd


def test_appsfolder_reusing_window_is_not_adopted_as_new(tmp_path, loaded, rows):
    existing = window(1)
    native = Native([existing])
    # New-window setting for a non-keyboard-supported app is represented by Calculator.
    existing.aumids = ("Calc.App",)
    settings = {"new_window_apps": ["Calculator"]}
    ctx = Context(registry(tmp_path, loaded, rows, native=native, settings=settings), owned={existing.hwnd: existing.pid})
    with pytest.raises(ExecutionError, match="existing windows were not adopted"):
        apps.open_app(ctx, {"app": "Calculator"})
    assert native.calls == [("launch", "Calc.App")]
    assert not any(event[0] == "adopt" for event in ctx.events)


@pytest.mark.parametrize("app_name,keys,exe", [
    ("Notepad", "ctrl+shift+n", r"C:\Apps\note.exe"),
    ("Browser", "ctrl+n", r"C:\Apps\browser.exe"),
])
def test_authorized_existing_window_newwindow_shortcut_is_scoped(tmp_path, loaded, rows, monkeypatch, app_name, keys, exe):
    existing, created = window(1, exe=exe), window(2, exe=exe)
    native = Native([existing])
    settings = {"new_window_apps": [app_name], "browser_apps": ["Browser"]}
    ctx = Context(registry(tmp_path, loaded, rows, native=native, settings=settings), owned={existing.hwnd: existing.pid})
    def press(context, target, actual_keys):
        assert context.native.foreground() == existing.hwnd
        context.guard(existing)
        assert target == existing and actual_keys == keys
        native.calls.append(("press", keys))
        native.items.append(created)
    monkeypatch.setitem(sys.modules, "voicebar.executor.input", SimpleNamespace(press_window=press))
    apps.open_app(ctx, {"app": app_name})
    assert not any(call[0] == "launch" for call in native.calls)
    assert ctx.owned[created.hwnd] == created.pid


def test_unowned_notepad_newwindow_request_declines_before_appsfolder_or_keys(tmp_path, loaded, rows):
    native = Native([window(1)])
    ctx = Context(registry(tmp_path, loaded, rows, native=native, settings={"new_window_apps": ["Notepad"]}), owned={})
    with pytest.raises(ExecutionError, match="unowned matching windows"):
        apps.open_app(ctx, {"app": "Notepad"})
    assert native.calls == []


def test_close_all_matching_top_level_windows_but_only_owned(tmp_path, loaded, rows):
    first, second, untouched = window(1), window(2), window(3)
    native = Native([first, second, untouched])
    ctx = Context(registry(tmp_path, loaded, rows, native=native), owned={first.hwnd: first.pid, second.hwnd: second.pid})
    assert apps.close_app(ctx, {"app": "Notepad"}) == "Closed 2 Notepad window(s)"
    assert native.calls == [("close", first.hwnd), ("close", second.hwnd)]
    assert native.items == [untouched]


def test_unsaved_window_remains_open_and_no_kill_or_discard_is_attempted(tmp_path, loaded, rows):
    existing = window(1)
    native = Native([existing])
    native.keep_open.add(existing.hwnd)
    ctx = Context(registry(tmp_path, loaded, rows, native=native), owned={existing.hwnd: existing.pid})
    with pytest.raises(ExecutionError, match="save or discard changes manually.*No process was terminated"):
        apps.close_app(ctx, {"app": "Notepad"})
    assert native.calls == [("close", existing.hwnd)]
    assert native.items == [existing]


def test_all_close_targets_are_guarded_before_first_mutation(tmp_path, loaded, rows):
    first, second = window(1), window(2)
    native = Native([first, second])
    ctx = Context(registry(tmp_path, loaded, rows, native=native))
    ctx.blocked_pids.add(second.pid)
    with pytest.raises(ExecutionError, match="Elevated"):
        apps.close_app(ctx, {"app": "Notepad"})
    assert native.calls == []


def test_foreground_activation_must_be_confirmed(tmp_path, loaded, rows, monkeypatch):
    existing = window(1)
    native = Native([existing])
    native.active = 0
    monkeypatch.setattr(native, "activate", lambda hwnd, timeout: None)
    ctx = Context(registry(tmp_path, loaded, rows, native=native))
    with pytest.raises(ExecutionError, match="did not grant foreground"):
        apps.switch_app(ctx, {"app": "Notepad"})


def add_ambiguous_app(loaded, rows):
    for identity in ("Google.Antigravity", "electron.app.Antigravity"):
        loaded.installed_apps.append(InstalledApp("Antigravity", identity))
        loaded.catalog.apps.append("Antigravity")
        rows.append({"Name": "Antigravity", "AppID": identity,
                     "Executable": r"C:\Apps\antigravity.exe", "Arguments": ""})


def test_inverse_identity_ignores_unrelated_duplicate_names(tmp_path, loaded, rows):
    add_ambiguous_app(loaded, rows)
    result = registry(tmp_path, loaded, rows)
    assert result.name_for_window(window(1, aumids=["Calc.App"])) == "Calculator"
    assert result.name_for_window(window(2)) == "Notepad"
    assert result.name_for_window(window(3, aumids=["Google.Antigravity"])) is None
    assert result.name_for_window(window(4, exe=r"C:\Apps\antigravity.exe")) is None


@pytest.mark.parametrize("aumids, executable, expected", [
    (("Note.App", "Calc.App"), r"C:\Apps\note.exe", None),
    (("Calc.App",), r"C:\Apps\note.exe", "Calculator"),
    (("Note.App.Profile",), r"c:\APPS\NOTE.exe", "Notepad"),
    ((), r"D:\Other\note.exe", None),
])
def test_inverse_identity_preserves_forward_identity_rules(tmp_path, loaded, rows, aumids, executable, expected):
    result = registry(tmp_path, loaded, rows)
    assert result.name_for_window(window(1, exe=executable, aumids=aumids)) == expected


@pytest.mark.parametrize("shared_owner, arguments", [(True, ""), (False, "--different-experience")])
def test_inverse_identity_rejects_shared_host_or_shortcut_arguments(tmp_path, loaded, rows, shared_owner, arguments):
    if shared_owner:
        rows.append({"Name": "Unknown", "AppID": "Unknown.App",
                     "Executable": r"C:\Apps\note.exe", "Arguments": "--tool"})
    rows[0]["Arguments"] = arguments
    assert registry(tmp_path, loaded, rows).name_for_window(window(1)) is None


@pytest.mark.parametrize("entrypoint", ["executor", "runtime"])
@pytest.mark.parametrize("change", [None, "focus", "pid"])
def test_foreground_lookup_uses_actual_registry_and_rechecks_identity(
        tmp_path, loaded, rows, monkeypatch, entrypoint, change):
    from voicebar.executor import app_actions, common
    from voicebar.ui.controller import RuntimeController

    add_ambiguous_app(loaded, rows)
    native = Native([window(1, aumids=["Calc.App"])])
    app_registry = registry(tmp_path, loaded, rows, native=native)
    original = app_registry.name_for_window

    def lookup(observed):
        result = original(observed)
        if change == "focus":
            native.active = 2
        elif change == "pid":
            native.items = [window(1, pid=999, aumids=["Calc.App"])]
        return result

    monkeypatch.setattr(app_registry, "name_for_window", lookup)
    context = common.Context(tmp_path, loaded=loaded, native=native, apps=app_registry,
                             settings={}, persist=False)
    if entrypoint == "executor":
        monkeypatch.setattr(app_actions, "Context", lambda *args, **kwargs: context)
        result = app_actions.foreground_app(tmp_path, loaded, settings={})
    else:
        monkeypatch.setattr(common, "Context", lambda *args, **kwargs: context)
        runtime = object.__new__(RuntimeController)
        runtime.root, runtime.loaded = tmp_path, loaded
        runtime._foreground_app, runtime._executor_settings = None, {}
        runtime.session = SimpleNamespace(gate=SimpleNamespace(
            conversational=True, asleep=False, until=float("inf")))
        result = runtime._foreground()
    assert result == ("Calculator" if change is None else None)
    assert native.calls == []
