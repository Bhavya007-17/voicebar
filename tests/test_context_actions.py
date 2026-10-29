"""Context actions use owned in-process fakes; no desktop app is opened or touched."""
from types import SimpleNamespace

import pytest

from voicebar.calculator import keypad_sequence
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.executor import app_actions, uia
from voicebar.executor.common import Context, ExecutionError


@pytest.mark.parametrize("spoken, expected", [
    ("2+3", ("2", "+", "3")),
    ("two plus three", ("2", "+", "3")),
    ("Two plus THREE.", ("2", "+", "3")),
    ("twenty-one minus four", ("2", "1", "-", "4")),
    ("one hundred and two divided by three", ("1", "0", "2", "/", "3")),
    ("2.5 times four", ("2", ".", "5", "*", "4")),
    ("zero point zero five multiplied by 2", ("0", ".", "0", "5", "*", "2")),
    (".5 + .3", ("0", ".", "5", "+", "0", ".", "3")),
    ("negative two plus minus three", ("2", "negate", "+", "3", "negate")),
    ("-2--3", ("2", "negate", "-", "3", "negate")),
    ("12 ÷ 4", ("1", "2", "/", "4")),
    ("3 × 4", ("3", "*", "4")),
])
def test_arithmetic_speech_has_closed_keypad_meaning(spoken, expected):
    assert keypad_sequence(spoken) == expected


@pytest.mark.parametrize("spoken", [
    "", "two", "two plus", "plus two", "two plus three and open notepad",
    "two plus three then press enter", "2 + 3 * 4", "(2+3)",
    "__import__('os')", "2;3", "2e4+1", "2 + 3\0", "٢+٣", "2 ** 3",
    "two three plus four", "twenty zero plus five", "one hundred and plus two",
    "one point twenty plus two", "one point two point three plus four",
    "1234567890123 + 1", "2 + 3..", "2 + 3 " + " " * 160,
])
def test_arithmetic_refuses_incomplete_ambiguous_or_non_arithmetic_text(spoken):
    assert keypad_sequence(spoken) is None


def row(name, role="button", automation_id=None, enabled=True):
    return {"name": name, "role": role, "automation_id": automation_id or name,
            "enabled": enabled, "clickable": True}


class Native:
    def __init__(self):
        self.items = [SimpleNamespace(hwnd=1, pid=101, app="Calculator"),
                      SimpleNamespace(hwnd=2, pid=102, app="Browser"),
                      SimpleNamespace(hwnd=3, pid=103, app="Editor")]
        self.focus = 1
        self.activations = []

    def foreground(self):
        return self.focus

    def windows(self):
        return self.items

    def is_window(self, hwnd, pid):
        return any((item.hwnd, item.pid) == (hwnd, pid) for item in self.items)

    def integrity(self, pid):
        return 0

    def current_integrity(self):
        return 0

    def activate(self, hwnd, timeout):
        self.activations.append(hwnd)
        self.focus = hwnd


class Automation:
    def __init__(self):
        self.rows = [row("Standard Calculator mode", "text", "Header")]
        self.clicks = []
        self.queries = []
        self.after_find = lambda: None

    def find(self, hwnd, selector):
        self.queries.append((hwnd, selector))
        result = [item for item in self.rows if uia.matches(item, selector)]
        self.after_find()
        return result

    def click(self, hwnd, selector, guard):
        result = uia.unique(self.rows, selector)
        guard()
        self.clicks.append((hwnd, selector))
        return result

    def close(self):
        pass


@pytest.fixture
def context(tmp_path):
    native = Native()
    loaded = LoadedCatalog(Catalog(apps=["Calculator", "Browser", "Editor"]), [], {}, [])
    registry = SimpleNamespace(
        matches=lambda window, name: window.app == name,
        name_for_window=lambda window: window.app,
        browser=lambda window: window.app == "Browser",
    )
    ctx = Context(tmp_path, loaded=loaded, native=native, apps=registry,
                  settings={}, persist=False, owned={item.hwnd: item.pid for item in native.items})
    ctx.ownership_record = None
    ctx.automation = Automation()
    ctx.sent = []
    ctx.input_backend = SimpleNamespace(modifiers_held=lambda: False, send=ctx.sent.append)
    yield ctx
    ctx.close()


def action(name, app="active"):
    return {"action": "app_action", "app": app, "name": name}


def test_calculator_uses_only_clear_digits_operator_equals(context):
    detail = app_actions.app_action(context, action("calculate:two plus three", "Calculator"))
    assert [events[0][1] for events in context.sent] == [0x1B, ord("2"), 0x6B, ord("3"), 0x0D]
    assert context.native.activations == []
    assert context.last_app == "Calculator"
    assert "result is not observable" in detail


def test_stale_calculator_context_never_activates_or_types(context):
    context.native.focus = 3
    with pytest.raises(ExecutionError, match="no longer foreground"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert context.native.activations == context.sent == context.automation.queries == []


@pytest.mark.parametrize("mode", ["Programmer Calculator mode", "Date Calculation mode", "Converter"])
def test_calculator_other_modes_receive_no_keys(context, mode):
    context.automation.rows = [row(mode, "text", "Header")]
    with pytest.raises(ExecutionError, match="Standard or Scientific"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert context.sent == []


def test_calculator_rechecks_foreground_after_read(context):
    context.automation.after_find = lambda: setattr(context.native, "focus", 3)
    with pytest.raises(ExecutionError, match="Foreground changed"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert context.sent == []


def test_calculator_focus_loss_during_typing_stops_remaining_keys(context):
    def send(events):
        context.sent.append(events)
        context.native.focus = 3
    context.input_backend.send = send
    with pytest.raises(ExecutionError, match="Foreground changed"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert len(context.sent) == 1
    assert context.last_app is None


def test_invalid_calculation_refused_during_preflight(context):
    with pytest.raises(ExecutionError, match="complete arithmetic"):
        app_actions.definition(context, action("calculate:2 + 3 and open editor", "Calculator"))
    assert context.sent == context.automation.queries == []


def test_calculator_physical_modifier_prevents_clear_and_all_other_keys(context):
    context.input_backend.modifiers_held = lambda: True
    with pytest.raises(ExecutionError, match="modifier"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert context.sent == []


def test_unowned_calculator_is_refused_before_screen_read(context):
    context.owned = {}
    with pytest.raises(ExecutionError, match="did not open"):
        app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert context.sent == context.automation.queries == []


def test_generic_click_can_explicitly_disambiguate_role(context):
    context.automation.rows = [row("Calendar", "hyperlink"), row("Calendar", "button")]
    app_actions.app_action(context, action("click_role:hyperlink:calendar"))
    assert context.automation.clicks == [(1, {"name": "calendar", "role": "hyperlink"})]


def test_duplicate_same_role_labels_are_still_ambiguous(context):
    context.automation.rows = [row("Calendar"), row(" Calendar ")]
    with pytest.raises(uia.ControlMatchError, match="found 2"):
        app_actions.app_action(context, action("click_role:button:CALENDAR"))
    assert context.automation.clicks == []


def test_click_does_not_erase_meaningful_label_punctuation(context):
    context.automation.rows = [row("Save..."), row("Save")]
    app_actions.app_action(context, action("click:Save"))
    assert context.automation.clicks == [(1, {"name": "Save"})]


def test_unsupported_explicit_role_does_not_fall_back_to_unqualified_click(context):
    with pytest.raises(ExecutionError, match="Unknown configured action"):
        app_actions.app_action(context, action("click_role:any:Calendar"))
    assert context.automation.clicks == []


def test_active_browser_tab_selection_uses_observed_exact_label(context):
    context.native.focus = 2
    context.automation.rows = [row("Course Calendar", "tabitem")]
    app_actions.app_action(context, action("switch_tab:Course Calendar"))
    assert context.automation.clicks == [(2, {"name": "Course Calendar", "role": "tabitem"})]
    assert context.native.activations == []


@pytest.mark.parametrize("name", ["switch_tab:Course Calendar", "browser_window:new"])
def test_browser_actions_refuse_non_browser_before_scanning_or_typing(context, name):
    with pytest.raises(ExecutionError, match="not a verified browser"):
        app_actions.app_action(context, action(name))
    assert context.sent == context.automation.queries == context.automation.clicks == []


def test_browser_new_window_uses_new_window_not_new_tab_shortcut(context):
    context.native.focus = 2
    app_actions.app_action(context, action("browser_window:new"))
    assert len(context.sent) == 1
    assert [event[1] for event in context.sent[0]] == [0x11, ord("N"), ord("N"), 0x11]


def test_foreground_context_covers_apps_without_profiles_without_screen_scan(context, monkeypatch):
    context.native.focus = 3
    monkeypatch.setattr(app_actions, "Context", lambda *args, **kwargs: context)
    assert app_actions.foreground_app(context.root, context.loaded, settings={}) == "Editor"
    assert context.native.activations == context.sent == context.automation.queries == []


def test_ambiguous_foreground_app_is_not_remembered(context, monkeypatch):
    context.apps.name_for_window = lambda window: None
    monkeypatch.setattr(app_actions, "Context", lambda *args, **kwargs: context)
    assert app_actions.foreground_app(context.root, context.loaded, settings={}) is None
    assert context.last_app is None
