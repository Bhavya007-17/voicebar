"""App actions persist pronoun targets through real state writes, with fake UI only."""

import json
from types import SimpleNamespace

import pytest

from voicebar import cli, executor, ptt
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.executor.common import Context, ExecutionError, read_state
from voicebar.executor.uia import matches, unique
from voicebar.parser import parse


class Native:
    def __init__(self):
        self.windows_by_app = {
            name: SimpleNamespace(hwnd=index, pid=index + 100)
            for index, name in enumerate(("Claude", "Google Chrome", "Notepad"), 10)
        }
        self.focus = self.windows_by_app["Notepad"].hwnd

    def foreground(self):
        return self.focus

    def windows(self):
        return list(self.windows_by_app.values())

    def is_window(self, hwnd, pid):
        return any((window.hwnd, window.pid) == (hwnd, pid) for window in self.windows())

    def integrity(self, _pid):
        return 0x2000

    def current_integrity(self):
        return 0x2000

    def activate(self, hwnd, _timeout):
        self.focus = hwnd

    def wait(self, predicate, _timeout, _description):
        result = predicate()
        if not result:
            raise ExecutionError("Named control did not appear")
        return result

    def shutdown(self):
        pass


def control(name, role="button"):
    return {"name": name, "role": role, "automation_id": name, "enabled": True}


class Automation:
    def __init__(self):
        self.rows = [control("New chat"), control("Ready"), control("Notes", "tabitem")]
        self.effects = []
        self.fail_click = False

    def find(self, _hwnd, selector):
        return [row for row in self.rows if matches(row, selector)]

    def click(self, hwnd, selector, guard):
        row = unique(self.rows, selector)
        guard()
        if self.fail_click:
            raise ExecutionError("Fake provider refused action")
        self.effects.append((hwnd, row["name"]))
        return row

    def close(self):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    native, automation = Native(), Automation()
    state_file = tmp_path / "owned-session.json"
    initial = {"last_app": "Notepad", "owned": {
        str(window.hwnd): window.pid for window in native.windows()
    }}
    state_file.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setenv("VOICEBAR_TEST_SESSION", str(state_file))
    catalog = Catalog(
        apps=list(native.windows_by_app), aliases={"chrome": "Google Chrome"},
        app_actions={"Claude": {"new_chat": {
            "say": ["new chat"], "do": [
                {"click": {"name": "New chat", "role": "button"}},
                {"wait": {"name": "Ready", "role": "button", "timeout": 1}},
            ],
        }}},
    )
    loaded = LoadedCatalog(catalog, [], {}, [])
    registry = SimpleNamespace(find=lambda app, owned=None: [native.windows_by_app[app]])

    def context(root, **kwargs):
        ctx = Context(root, native=native, apps=registry,
                      settings={"window_timeout_seconds": 1}, **kwargs)
        ctx.automation = automation
        return ctx

    monkeypatch.setattr(cli, "load_catalog", lambda root, **kwargs: loaded)
    monkeypatch.setattr(executor, "Context", context)

    def run(text):
        return cli.main(["run", "--active-app", "Notepad", text], project_root=tmp_path)

    def assert_target(app):
        # CLI reads read_state; standalone PTT independently uses _default_state.
        assert read_state(tmp_path)["last_app"] == app
        assert ptt._default_state(tmp_path) == app
        assert parse("close it", catalog, last_app=ptt._default_state(tmp_path)) == [
            {"action": "window", "op": "close", "target": app},
        ]
        assert read_state(tmp_path)["owned"] == initial["owned"]

    return SimpleNamespace(run=run, assert_target=assert_target, automation=automation,
                           state_file=state_file, initial=initial)


@pytest.mark.parametrize("text,app", [
    ("new chat in Claude", "Claude"),
    ("switch to the tab named Notes in chrome", "Google Chrome"),
])
def test_successful_named_action_persists_cli_and_ptt_pronoun_target(runtime, text, app):
    assert runtime.run(text) == 0
    assert runtime.automation.effects
    runtime.assert_target(app)


@pytest.mark.parametrize("text", ["new chat in Claude", "switch to tab Notes in chrome"])
def test_failed_named_click_preserves_previous_persisted_target(runtime, text):
    runtime.automation.fail_click = True
    assert runtime.run(text) == 1
    assert runtime.automation.effects == []
    runtime.assert_target("Notepad")


def test_failure_after_an_earlier_effect_does_not_remember_uncompleted_action(runtime):
    runtime.automation.rows = [row for row in runtime.automation.rows if row["name"] != "Ready"]
    assert runtime.run("new chat in Claude") == 1
    assert len(runtime.automation.effects) == 1
    runtime.assert_target("Notepad")


@pytest.mark.parametrize("matching_tabs", [0, 2])
def test_missing_or_ambiguous_chrome_tab_does_not_change_persisted_target(runtime, matching_tabs):
    runtime.automation.rows = [control("Notes", "tabitem") for _ in range(matching_tabs)]
    assert runtime.run("switch to tab Notes in chrome") == 1
    assert runtime.automation.effects == []
    runtime.assert_target("Notepad")


@pytest.mark.parametrize("fail", [False, True])
def test_generic_active_click_preserves_previous_named_app_on_success_or_failure(runtime, fail):
    runtime.automation.fail_click = fail
    assert runtime.run("click New chat") == int(fail)
    assert len(runtime.automation.effects) == int(not fail)
    runtime.assert_target("Notepad")
