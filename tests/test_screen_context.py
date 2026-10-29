"""Accessibility-grounded resolution and HWND/PID checks with in-process fakes."""
from types import SimpleNamespace

import pytest

from voicebar import screen
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.executor import app_actions, common, uia
from voicebar.executor.input import _foreground_guard


def control(name="Calendar", role="hyperlink", *, enabled=True):
    return {"name": name, "role": role, "automation_id": "observed-control", "enabled": enabled}


class Native:
    def __init__(self):
        self.current = SimpleNamespace(hwnd=11, pid=101)
        self.focus = self.current.hwnd

    def foreground(self):
        return self.focus

    def windows(self):
        return [self.current]

    def is_window(self, hwnd, pid):
        return (hwnd, pid) == (self.current.hwnd, self.current.pid)

    def integrity(self, pid):
        return 0

    def current_integrity(self):
        return 0


class Automation:
    def __init__(self):
        self.rows = [control()]
        self.queries = []
        self.clicks = []
        self.after_find = lambda: None
        self.before_click = lambda: None

    def find(self, hwnd, selector):
        self.queries.append((hwnd, selector))
        rows = [row for row in self.rows if uia.matches(row, selector)]
        self.after_find()
        return rows

    def snapshot(self, hwnd):
        return [dict(row, clickable=True) for row in self.rows]

    def click(self, hwnd, selector, guard):
        selected = uia.unique(self.rows, selector)
        self.before_click()
        guard()
        self.clicks.append((hwnd, selector))
        return selected

    def close(self):
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    loaded = LoadedCatalog(Catalog(), [], {}, [])
    native = Native()
    ctx = common.Context(tmp_path, loaded=loaded, native=native, apps=SimpleNamespace(),
                         owned={11: 101}, settings={}, persist=False)
    ctx.ownership_record = None
    ctx.automation = Automation()
    monkeypatch.setattr(common, "Context", lambda *args, **kwargs: ctx)
    return ctx


def resolve(context, text="open Calendar"):
    return screen.resolve_control(text, context.root, context.loaded, settings={})


@pytest.mark.parametrize("phrase", ["open Calendar", "show Calendar", "select Calendar", "go to Calendar"])
def test_unique_observed_control_supplies_exact_action_and_window_identity(context, phrase):
    assert resolve(context, phrase) == ([{
        "action": "app_action", "app": "active", "name": "click_role:hyperlink:Calendar",
    }], (11, 101))
    assert context.automation.clicks == []
    assert context.started_at is None


def test_resolution_preserves_observed_label_case_and_punctuation(context):
    context.automation.rows = [control("Project: Today's Calendar")]
    result = resolve(context, "open project: today's calendar")
    assert result[0][0]["name"] == "click_role:hyperlink:Project: Today's Calendar"


def test_absent_control_does_not_guess_related_names(context):
    context.automation.rows = [control("Calendar settings"), control("Academic Calendar")]
    assert resolve(context) is None
    assert context.automation.clicks == []


def test_ambiguous_label_across_control_roles_refuses_without_click(context):
    context.automation.rows = [control(), control("CALENDAR", "button")]
    with pytest.raises(uia.ControlMatchError, match="found 2") as failure:
        resolve(context)
    assert len(failure.value.candidates) == 2
    assert context.automation.clicks == []


def test_disabled_control_is_refused(context):
    context.automation.rows = [control(enabled=False)]
    with pytest.raises(uia.ControlMatchError, match="disabled"):
        resolve(context)
    assert context.automation.clicks == []


def test_non_actionable_control_role_is_not_upgraded_to_button(context):
    context.automation.rows = [control(role="text")]
    assert resolve(context) is None


@pytest.mark.parametrize("change", ["focus", "pid"])
def test_screen_query_does_not_return_plan_after_original_window_changes(context, change):
    def revoke():
        if change == "focus":
            context.native.focus = 22
        else:
            context.native.current = SimpleNamespace(hwnd=11, pid=202)
    context.automation.after_find = revoke
    with pytest.raises(common.ExecutionError, match="Foreground changed|identity changed"):
        resolve(context)
    assert context.automation.clicks == []


@pytest.mark.parametrize("text", ["we should open Calendar", "today is Wednesday", "open", "open " + "x" * 201])
def test_unrecognized_request_never_reads_screen(context, text):
    assert resolve(context, text) is None
    assert context.automation.queries == []


def test_required_foreground_allows_exact_live_identity(context):
    context.required_foreground = (11, 101)
    assert context.guard(context.native.current) is context.native.current


@pytest.mark.parametrize("change", ["focus", "pid", "target"])
def test_required_foreground_refuses_retargeting_before_execution(context, change):
    context.required_foreground = (11, 101)
    window = context.native.current
    if change == "focus":
        context.native.focus = 22
    elif change == "pid":
        context.native.current = SimpleNamespace(hwnd=11, pid=202)
        window = context.native.current
    else:
        window = SimpleNamespace(hwnd=22, pid=202)
    with pytest.raises(common.ExecutionError, match="observed target window changed"):
        context.guard(window)
    assert context.started_at is None


def test_required_foreground_rechecks_identity_between_guards(context):
    context.required_foreground = (11, 101)
    window = context.guard(context.native.current)
    context.native.current = SimpleNamespace(hwnd=11, pid=202)
    with pytest.raises(common.ExecutionError, match="identity changed"):
        context.guard(window)


@pytest.mark.parametrize("change", ["focus", "pid"])
def test_grounded_click_refuses_window_change_during_final_lookup(context, change):
    plan, identity = resolve(context)
    context.required_foreground = identity
    def revoke():
        if change == "focus":
            context.native.focus = 22
        else:
            context.native.current = SimpleNamespace(hwnd=11, pid=202)
    context.automation.before_click = revoke
    with pytest.raises(common.ExecutionError, match="observed target window changed|identity changed"):
        app_actions.app_action(context, plan[0])
    assert context.automation.clicks == []
    assert context.started_at is None


def test_foreground_guard_rechecks_focus_after_integrity_read(context):
    context.required_foreground = (11, 101)
    def integrity(pid):
        context.native.focus = 22
        return 0
    context.native.integrity = integrity
    with pytest.raises(common.ExecutionError, match="Foreground changed"):
        _foreground_guard(context, context.native.current)


def visual(context, *, choose=lambda *args, **kwargs: 1, frame=True, authorized=lambda: True):
    frames = SimpleNamespace(get=lambda *args: SimpleNamespace(png=b"local-image") if frame else None)
    selector = SimpleNamespace(select=choose)
    return screen.resolve_control("click the calendar icon", context.root, context.loaded,
                                  settings={}, frames=frames, selector=selector,
                                  generation=4, authorized=authorized)


def test_visual_choice_only_selects_an_observed_action(context):
    calls = []

    def choose(text, image, candidates, **kwargs):
        calls.append((text, image, candidates))
        return 1

    plan, identity = visual(context, choose=choose)
    assert identity == (11, 101)
    assert plan == [{"action": "app_action", "app": "active", "name": "click_role:hyperlink:Calendar"}]
    assert calls[0][1] == b"local-image"
    assert calls[0][2] == [{"id": 1, "name": "Calendar", "role": "hyperlink"}]
    assert context.automation.clicks == []


@pytest.mark.parametrize("choice", [None, True, 0, 2, "1", {"action": "press", "keys": "enter"}])
def test_visual_invalid_or_uncertain_choice_cannot_become_action(context, choice):
    with pytest.raises(uia.ControlMatchError, match="unclear"):
        visual(context, choose=lambda *args, **kwargs: choice)


def test_exact_screen_match_never_calls_vision(context):
    def unexpected(*args, **kwargs):
        pytest.fail("exact match must avoid model latency")
    result = screen.resolve_control("click Calendar", context.root, context.loaded,
                                    selector=SimpleNamespace(select=unexpected))
    assert result[0][0]["name"] == "click_role:hyperlink:Calendar"


def test_missing_screen_frame_refuses_before_inference(context):
    with pytest.raises(uia.ControlMatchError, match="No current screen"):
        visual(context, frame=False)


@pytest.mark.parametrize("change", ["focus", "pid", "controls", "authorization"])
def test_visual_selection_is_discarded_when_observation_changes(context, change):
    allowed = [True]

    def choose(*args, **kwargs):
        if change == "focus":
            context.native.focus = 22
        elif change == "pid":
            context.native.current = SimpleNamespace(hwnd=11, pid=202)
        elif change == "controls":
            context.automation.rows = [control("Different page")]
        else:
            allowed[0] = False
        return 1

    with pytest.raises(common.ExecutionError):
        visual(context, choose=choose, authorized=lambda: allowed[0])
    assert context.automation.clicks == []


def test_visual_options_exclude_duplicates_disabled_unlabeled_and_nonactionable():
    rows = [dict(control(), clickable=True), dict(control("CALENDAR"), clickable=True),
            dict(control("Hidden", enabled=False), clickable=True),
            dict(control(""), clickable=True), dict(control("Text", role="text"), clickable=True),
            dict(control("Fake button", role="button"), clickable=False),
            dict(control("Observed", role="button"), clickable=True)]
    assert screen.visual_candidates(rows) == [{"id": 1, "name": "Observed", "role": "button"}]


def test_visual_result_refuses_changed_page_text_even_when_buttons_match(context):
    context.automation.rows.append(control("Assignment A", role="text"))
    def choose(*args, **kwargs):
        context.automation.rows[-1]["name"] = "Assignment B"
        return 1
    with pytest.raises(uia.ControlMatchError, match="controls changed"):
        visual(context, choose=choose)


def test_visual_result_requires_live_capture_after_inference(context):
    current = [SimpleNamespace(png=b"local-image")]
    def choose(*args, **kwargs):
        current[0] = None
        return 1
    with pytest.raises(uia.ControlMatchError, match="authorization changed"):
        screen.resolve_control("click the calendar icon", context.root, context.loaded,
            frames=SimpleNamespace(get=lambda *args: current[0]),
            selector=SimpleNamespace(select=choose))


@pytest.mark.parametrize("config", [{"enabled": "yes"}, {"timeout_ms": True},
                                    {"timeout_ms": 0}, {"model": "remote:cloud"}, {"host": "example.com"}])
def test_invalid_vision_config_is_rejected_without_io(config):
    with pytest.raises((ValueError, RuntimeError)):
        screen.vision_settings({"vision": config})
