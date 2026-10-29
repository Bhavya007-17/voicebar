"""Calculator clear uses one foreground-bound UIA action and optional readback."""

import pytest
from test_context_actions import action, row
from test_context_actions import context as _context

from voicebar.executor import app_actions
from voicebar.executor.common import ExecutionError
from voicebar.executor.uia import ControlMatchError

context = _context


def install_clear(context, result="0"):
    context.automation.rows.append(row("Clear", "button", "clearButton"))
    if result is not None:
        context.automation.rows.append(row("Display is " + result, "text", "CalculatorResults"))


def test_clear_clicks_exact_owned_button_and_reads_zero_without_keys(context):
    install_clear(context)
    assert app_actions.app_action(context, action("clear", "Calculator")) == "Calculator shows 0"
    assert context.automation.clicks == [(1, {"name": "Clear", "role": "button", "automation_id": "clearButton"})]
    assert context.native.activations == context.sent == []
    assert context.calculator_verification == {"status": "verified", "display": "0"}


def test_clear_does_not_activate_remembered_calculator_or_use_other_apps_clear(context):
    install_clear(context)
    context.native.focus = 3
    context.last_app = "Calculator"
    with pytest.raises(ExecutionError, match="no longer foreground"):
        app_actions.app_action(context, action("clear", "Calculator"))
    assert context.automation.clicks == context.automation.queries == context.native.activations == context.sent == []


@pytest.mark.parametrize("mode", ["Programmer Calculator mode", "Converter", "Date Calculation mode"])
def test_clear_refuses_unsupported_mode_before_click(context, mode):
    install_clear(context)
    context.automation.rows[0]["name"] = mode
    with pytest.raises(ExecutionError, match="Standard or Scientific"):
        app_actions.app_action(context, action("clear", "Calculator"))
    assert context.automation.clicks == context.sent == []


def test_clear_duplicate_controls_refuse_instead_of_guessing(context):
    install_clear(context)
    context.automation.rows.append(row("Clear", "button", "clearButton"))
    with pytest.raises(ControlMatchError, match="found 2"):
        app_actions.app_action(context, action("clear", "Calculator"))
    assert context.automation.clicks == context.sent == []


def test_clear_rejects_focus_change_while_reading_mode(context):
    install_clear(context)
    context.automation.after_find = lambda: setattr(context.native, "focus", 3)
    with pytest.raises(ExecutionError, match="Foreground changed"):
        app_actions.app_action(context, action("clear", "Calculator"))
    assert context.automation.clicks == context.sent == []


def test_clear_rejects_unowned_calculator_before_read(context):
    install_clear(context)
    context.owned = {}
    with pytest.raises(ExecutionError, match="did not open"):
        app_actions.app_action(context, action("clear", "Calculator"))
    assert context.automation.clicks == context.automation.queries == context.sent == []


@pytest.mark.parametrize("result", [None, "5", "Cannot divide by zero"])
def test_clear_unreadable_or_nonzero_display_is_unverified_without_replay(context, result):
    install_clear(context, result)
    assert "not confirmed" in app_actions.app_action(context, action("clear", "Calculator"))
    assert context.calculator_verification["status"] == "unverified"
    assert len(context.automation.clicks) == 1 and context.sent == []


def test_clear_late_zero_uses_bounded_predicate_and_never_clicks_twice(context):
    install_clear(context, "5")
    waits = []

    def wait(predicate, seconds, description):
        waits.append(seconds)
        context.automation.rows[-1]["name"] = "Display is 0"
        assert predicate()

    context.native.wait = wait
    assert app_actions.app_action(context, action("clear", "Calculator")) == "Calculator shows 0"
    assert waits == [1.0] and len(context.automation.clicks) == 1


def test_clear_focus_loss_after_click_reports_unverified_without_further_actions(context):
    install_clear(context)
    click = context.automation.click

    def lose_focus(hwnd, selector, guard):
        result = click(hwnd, selector, guard)
        context.native.focus = 3
        return result

    context.automation.click = lose_focus
    assert "not observable" in app_actions.app_action(context, action("clear", "Calculator"))
    assert context.calculator_verification["status"] == "unverified"
    assert len(context.automation.clicks) == 1 and context.sent == []
