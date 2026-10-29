"""App-action safety tests use owned in-process fakes, never desktop windows."""

import json
import threading
from types import SimpleNamespace

import pytest

from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.executor import execute, uia
from voicebar.executor.common import Context, ExecutionError

WINDOW = SimpleNamespace(hwnd=10, pid=20, class_name="OwnedFake", title="Owned calculator")


def test_same_process_contexts_preserve_separate_ownership_records(tmp_path):
    native = Native()
    loaded = LoadedCatalog(Catalog(apps=["Calculator"]), [], {}, [])
    contexts = [Context(tmp_path, loaded=loaded, native=native, apps=SimpleNamespace(),
                        settings={"window_timeout_seconds": 1}, owned={}, persist=False)
                for _ in range(2)]
    first, second = contexts
    try:
        first.adopt(WINDOW)
        second.save()
        assert first.ownership_record != second.ownership_record
        assert json.loads(first.ownership_record.read_text())["owned"] == {"10": 20}
        assert json.loads(second.ownership_record.read_text())["owned"] == {}
        assert not list((tmp_path / ".cache").glob("*.tmp"))
    finally:
        for context in contexts:
            context.close()


@pytest.fixture(autouse=True)
def isolated_uia_worker_lifecycle(monkeypatch):
    # A real provider timeout poisons this process until restart. Fake-only
    # tests isolate that latch while always releasing their controlled workers.
    monkeypatch.setattr(uia, "_UNSAFE", threading.Event())


def control(name="One", *, automation_id="num1Button", enabled=True, clickable=True):
    return {"name": name, "role": "button", "automation_id": automation_id,
            "keyboard_shortcut": "", "enabled": enabled, "clickable": clickable,
            "invokable": True, "selectable": False, "focusable": True,
            "hwnd": 0, "pid": 20, "depth": 1}


class Native:
    def __init__(self):
        self.focus = WINDOW.hwnd
        self.trace = []
        self.wait_calls = []

    def foreground(self):
        return self.focus

    def windows(self):
        return [WINDOW]

    def is_window(self, hwnd, pid):
        return (hwnd, pid) == (WINDOW.hwnd, WINDOW.pid)

    def integrity(self, pid):
        return 0x2000

    def current_integrity(self):
        return 0x2000

    def activate(self, hwnd, timeout):
        self.trace.append(("activate", hwnd))
        self.focus = hwnd

    def wait(self, predicate, timeout, description):
        self.wait_calls.append((timeout, description))
        value = predicate()
        if not value:
            raise ExecutionError("Named control did not appear before the deadline")
        return value

    def shutdown(self):
        pass


class Automation:
    def __init__(self, native, rows=None, before_click=None):
        self.native, self.rows = native, rows if rows is not None else [control()]
        self.before_click = before_click
        self.clicks = []

    def snapshot(self, hwnd):
        self.native.trace.append(("snapshot", hwnd))
        return self.rows

    def find(self, hwnd, selector):
        self.native.trace.append(("find", hwnd))
        return [row for row in self.rows if uia.matches(row, selector)]

    def click(self, hwnd, selector, guard):
        row = uia.unique(self.rows, selector)
        if self.before_click:
            self.before_click()
        guard()
        self.clicks.append((hwnd, selector))
        self.native.trace.append(("click", row["name"]))
        return row

    def close(self):
        pass


@pytest.fixture
def context(tmp_path):
    native = Native()
    catalog = Catalog(apps=["Calculator"], app_actions={"Calculator": {
        "macro": {"say": ["macro"], "do": [{"click": {"name": "One"}}]},
        "voice": {"say": ["voice"], "do": [{"click": {"name": "One"}}], "then": "sleep"},
    }})
    loaded = LoadedCatalog(catalog, [], {}, [])
    registry = SimpleNamespace(find=lambda app, owned=None: [WINDOW])
    ctx = Context(tmp_path, loaded=loaded, native=native, apps=registry,
                  settings={"window_timeout_seconds": .05}, persist=False,
                  owned={WINDOW.hwnd: WINDOW.pid}, cancel=threading.Event())
    ctx.ownership_record = None
    ctx.automation = Automation(native)
    try:
        yield ctx
    finally:
        ctx.close()


def action(name="macro", app="Calculator"):
    return {"action": "app_action", "app": app, "name": name}


def test_unknown_configured_tail_preflight_prevents_earlier_launch(context, monkeypatch):
    monkeypatch.setattr("voicebar.executor._handlers", lambda: pytest.fail("No handler may run"))
    with pytest.raises(ExecutionError, match="Unknown configured action"):
        execute([{"action": "open_app", "app": "Calculator"}, action("missing")], context=context)
    assert context.native.trace == []
    assert context.automation.clicks == []


@pytest.mark.parametrize("rows, expected_count", [
    ([control("Someone")], 0),
    ([control(), control(automation_id="anotherOne")], 2),
])
def test_generic_click_requires_one_exact_control_and_returns_candidates(context, rows, expected_count):
    context.automation.rows = rows
    results = execute([action("click:One", "active")], context=context)
    assert not results[0]["ok"]
    assert f"found {expected_count}" in results[0]["detail"]
    assert results[0]["candidates"] == [
        {key: row[key] for key in ("name", "role", "automation_id", "enabled")} for row in rows]
    assert context.automation.clicks == []
    assert context.started_at is None


def test_generic_click_normalizes_case_and_whitespace_without_substring_matching(context):
    result = execute([action("click:  ONE  ", "active")], context=context)
    assert result[0]["ok"]
    assert context.automation.clicks == [(10, {"name": "  ONE  "})]
    assert context.native.trace == [("click", "One")]


def test_focus_loss_during_lookup_prevents_click(context):
    context.automation.before_click = lambda: setattr(context.native, "focus", 999)
    result = execute([action()], context=context)
    assert not result[0]["ok"] and "Foreground changed" in result[0]["detail"]
    assert context.automation.clicks == []
    assert context.native.trace == [("activate", 10)]


def test_cancellation_during_lookup_prevents_click_and_following_steps(context):
    context.automation.before_click = context.cancel.set
    result = execute([action(), action()], context=context)
    assert [row["ok"] for row in result] == [False, False]
    assert "cancelled" in result[0]["detail"]
    assert "Skipped" in result[1]["detail"]
    assert context.automation.clicks == []


def test_unowned_foreground_is_refused_before_ui_automation(context):
    context.owned = {11: 20}
    result = execute([action("click:One", "active")], context=context)
    assert not result[0]["ok"] and "did not open" in result[0]["detail"]
    assert context.native.trace == []
    assert context.automation.clicks == []


def test_configured_steps_activate_then_run_in_declared_order(context):
    context.loaded.catalog.app_actions["Calculator"]["macro"]["do"] = [
        {"keys": "ctrl+n"}, {"type": "Go"},
        {"wait": {"name": "One", "timeout": .3}}, {"click": {"automation_id": "num1Button"}},
    ]

    def send(events):
        if events[0][1] == 0x11:
            assert [event[1] for event in events] == [0x11, 0x4E, 0x4E, 0x11]
            context.native.trace.append(("keys", "ctrl+n"))
        else:
            assert events[0][3] == 4 and events[1][3] == 6
            context.native.trace.append(("type", chr(events[0][2])))

    context.input_backend = SimpleNamespace(modifiers_held=lambda: False, send=send)
    result = execute([action()], context=context)
    assert result[0]["ok"]
    assert context.native.trace == [
        ("activate", 10), ("keys", "ctrl+n"), ("type", "G"), ("type", "o"),
        ("find", 10), ("click", "One"),
    ]
    assert context.native.wait_calls == [(.3, "UI control {'name': 'One', 'timeout': 0.3}")]


def test_wait_with_duplicate_controls_stops_before_next_click(context):
    context.automation.rows = [control(), control(automation_id="anotherOne")]
    context.loaded.catalog.app_actions["Calculator"]["macro"]["do"] = [
        {"wait": {"name": "One", "timeout": .3}}, {"click": {"name": "One"}},
    ]
    result = execute([action()], context=context)
    assert not result[0]["ok"] and "ambiguous" in result[0]["detail"]
    assert len(result[0]["candidates"]) == 2
    assert context.automation.clicks == []


def test_sleep_handoff_emitted_only_on_success_and_stops_plan_tail(context):
    result = execute([action("voice"), action()], context=context)
    assert result[0]["ok"] and result[0]["then"] == "sleep"
    assert not result[1]["ok"] and "Skipped" in result[1]["detail"]
    assert len(context.automation.clicks) == 1
    context.automation.rows = []
    failed = execute([action("voice")], context=context)
    assert not failed[0]["ok"] and "then" not in failed[0]


@pytest.mark.parametrize("change", ["focus", "cancel", "disabled"])
def test_uia_worker_rechecks_guard_after_pattern_lookup(context, monkeypatch, change):
    """Exercise the real request worker while replacing only COM boundaries."""
    effects, row = [], control(enabled=change != "disabled")
    library = SimpleNamespace(CoUninitialize=lambda: effects.append("released"))
    client = SimpleNamespace(ElementFromHandle=lambda hwnd: object())
    monkeypatch.setattr(uia.Automation, "_library", staticmethod(lambda: (library, object(), client)))
    monkeypatch.setattr(uia.Automation, "_controls", lambda *args: ([row], [object()]))
    monkeypatch.setattr(uia.Automation, "_details", lambda *args: row)

    def pattern(*args):
        if change == "focus":
            context.native.focus = 999
        elif change == "cancel":
            context.cancel.set()
        return SimpleNamespace(Invoke=lambda: effects.append("invoked")), "Invoke"

    monkeypatch.setattr(uia.Automation, "_pattern", staticmethod(pattern))
    context.automation = uia.Automation(timeout=1)
    result = execute([action()], context=context)
    assert not result[0]["ok"]
    assert "invoked" not in effects
    if change == "disabled":
        assert result[0]["candidates"] == [row]
    else:
        assert "Foreground changed" in result[0]["detail"] or "cancelled" in result[0]["detail"]
    context.automation.close()
    assert effects == ["released"]


@pytest.mark.parametrize("change", ["focus", "cancel"])
def test_wait_rechecks_authorization_after_provider_query(context, change):
    context.loaded.catalog.app_actions["Calculator"]["voice"]["do"] = [
        {"wait": {"name": "One", "timeout": .3}},
    ]
    find = context.automation.find

    def revoke(hwnd, selector):
        rows = find(hwnd, selector)
        if change == "focus":
            context.native.focus = 999
        else:
            context.cancel.set()
        return rows

    context.automation.find = revoke
    result = execute([action("voice")], context=context)
    assert not result[0]["ok"] and "then" not in result[0]


def test_uia_provider_finishing_after_timeout_cannot_invoke(context, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    effects = []
    library = SimpleNamespace(CoUninitialize=lambda: effects.append("released"))
    client = SimpleNamespace(ElementFromHandle=lambda hwnd: object())
    monkeypatch.setattr(uia.Automation, "_library", staticmethod(lambda: (library, object(), client)))
    monkeypatch.setattr(uia.Automation, "_controls", lambda *args: ([control()], [object()]))
    monkeypatch.setattr(uia.Automation, "_details", lambda *args: control())

    def blocked_pattern(*args):
        entered.set()
        assert release.wait(2), "Test did not release its fake provider"
        return SimpleNamespace(Invoke=lambda: effects.append("invoked")), "Invoke"

    monkeypatch.setattr(uia.Automation, "_pattern", staticmethod(blocked_pattern))
    context.automation = uia.Automation(timeout=.05)
    try:
        result = execute([action()], context=context)
        assert entered.is_set()
        assert not result[0]["ok"] and "timed out" in result[0]["detail"]
        assert effects == []
        with pytest.raises(ExecutionError, match="closed"):
            context.automation.snapshot(WINDOW.hwnd)
        with pytest.raises(ExecutionError, match="restart Voicebar"):
            uia.Automation(timeout=1)
    finally:
        release.set()
        context.automation.close()
    assert effects == ["released"]
