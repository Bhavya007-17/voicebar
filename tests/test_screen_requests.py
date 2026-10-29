"""Named control requests retain roles and clarify missing references."""
from types import SimpleNamespace

import pytest
from test_screen_context import context as _context
from test_screen_context import control

from voicebar import screen
from voicebar.executor.uia import ControlMatchError

context = _context


@pytest.mark.parametrize("text,expected", [
    ("Uh, click on the tab Memory.", ("Memory", "tabitem")),
    ("click the button Bookmarks", ("Bookmarks", "button")),
    ("please select the checkbox Enable preview", ("Enable preview", "checkbox")),
    ("click on Bookmarks", ("Bookmarks", None)),
    ("I said click Bookmarks", (None, None)),
])
def test_label_and_role_are_extracted_from_direct_request(text, expected):
    assert screen.requested_control(text) == expected


def test_role_resolves_identical_labels_without_model_or_guess(context):
    context.automation.rows = [control("Memory", role="button"), control("Memory", role="tabitem")]
    result = screen.resolve_control("click the tab Memory", context.root, context.loaded)
    assert result == ([{"action": "app_action", "app": "active", "name": "click_role:tabitem:Memory"}], (11, 101))
    assert not context.automation.clicks


def test_unspecified_duplicate_label_asks_before_any_action(context):
    context.automation.rows = [control("Memory", role="button"), control("Memory", role="tabitem")]
    with pytest.raises(ControlMatchError, match="Which 'Memory' control"):
        screen.resolve_control("click Memory", context.root, context.loaded)
    assert not context.automation.clicks


@pytest.mark.parametrize("reference", ["this", "that", "it", "this one", "that one"])
def test_unbound_reference_asks_without_screen_query_or_model(context, reference):
    def unexpected(*args, **kwargs):
        pytest.fail("An unbound reference must not invoke vision")
    with pytest.raises(ControlMatchError, match="Which control"):
        screen.resolve_control(f"click {reference}", context.root, context.loaded,
                               selector=SimpleNamespace(select=unexpected))
    assert not context.automation.queries and not context.automation.clicks


@pytest.mark.parametrize("phrase", [
    "click that?", "click this!", "click the button this", "click the button that?",
    "click on the tab this one!", "select the checkbox it", "choose the field that one.",
])
def test_role_or_punctuation_does_not_identify_an_unbound_reference(context, phrase):
    def unexpected(*args, **kwargs):
        pytest.fail("A role or punctuation cannot authorize an unbound screen target")

    with pytest.raises(ControlMatchError, match="Which control"):
        screen.resolve_control(phrase, context.root, context.loaded,
                               selector=SimpleNamespace(select=unexpected))
    assert not context.automation.queries and not context.automation.clicks


def test_reference_words_inside_an_identifiable_label_remain_available(context):
    context.automation.rows = [control("This PC", role="button")]
    result = screen.resolve_control("click the button This PC", context.root, context.loaded)
    assert result[0][0]["name"] == "click_role:button:This PC"
    assert not context.automation.clicks


@pytest.mark.parametrize("names", [
    ["Memory add", "Memory recall", "Open memory"],
    ["Memory add", "Memory add", "Open memory"],
])
def test_partial_label_ambiguity_refuses_before_model_selection(context, names):
    context.automation.rows = [control(name, role="button") for name in names]
    frames = SimpleNamespace(get=lambda *args: SimpleNamespace(png=b"frame"))

    def unexpected(*args, **kwargs):
        pytest.fail("A model must not resolve explicit label ambiguity")

    with pytest.raises(ControlMatchError, match="Which 'memory' control") as failure:
        screen.resolve_control("Uh, click on memory", context.root, context.loaded,
                               frames=frames, selector=SimpleNamespace(select=unexpected))
    assert len(failure.value.candidates) == len(names)
    assert not context.automation.clicks


def test_full_label_resolves_after_partial_label_clarification(context):
    context.automation.rows = [control("Memory add", role="button"),
                               control("Open memory", role="button")]
    result = screen.resolve_control("click Open memory", context.root, context.loaded)
    assert result[0][0]["name"] == "click_role:button:Open memory"


def test_partial_ambiguity_uses_whole_words_requested_role_and_actionability():
    rows = [dict(control("Memory add", "button"), clickable=True),
            dict(control("Memory tab", "tabitem"), clickable=True),
            dict(control("Memory recall", "button", enabled=False), clickable=True),
            dict(control("Memory text", "text"), clickable=True),
            dict(control("Memory unavailable", "button"), clickable=False),
            dict(control("Memorymap", "button"), clickable=True)]
    assert screen.label_alternatives("memory", rows, "button") == rows[:1]
    assert screen.label_alternatives(None, rows) == []


def test_bound_probe_refuses_other_foreground_before_any_accessibility_query(context):
    from voicebar.executor.common import ExecutionError
    with pytest.raises(ExecutionError, match="observed target window changed"):
        screen.resolve_control("click Calendar", context.root, context.loaded,
                               required_foreground=(22, 202))
    assert not context.automation.queries and not context.automation.clicks


@pytest.mark.parametrize("phrase", ["click the tab Memory", "click the button Memory"])
def test_explicit_role_requests_use_observed_labels_before_execution(phrase):
    from test_conversation import harness
    calls = []
    role = "button" if "button" in phrase else "tabitem"
    observed = {"action": "app_action", "app": "active",
                "name": f"click_role:{role}:There's nothing saved in memory. Memory"}

    def ground(text):
        calls.append(text)
        return [observed]

    session = harness(grounded_resolver=ground)
    result = session.hear("computer " + phrase, 10)
    assert result["tier"] == "screen" and result["reason"] == "run"
    assert calls == [phrase] and session.calls == [observed]


@pytest.mark.parametrize("phrase", ["click Memory", "click the tab Memory"])
def test_unresolved_screen_request_cannot_execute_unbound_parser_action(phrase):
    from test_conversation import harness
    models = []
    session = harness(grounded_resolver=lambda text: None, resolver=lambda *args: models.append(args))
    result = session.hear("computer " + phrase, 10)
    assert result["reason"] == "unresolved" and result["plan"] is None
    assert not session.calls and not models
