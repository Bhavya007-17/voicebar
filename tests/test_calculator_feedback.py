"""Observed Calculator outcomes, using in-process owned-window fakes."""
from decimal import Decimal

import pytest
from test_context_actions import action, row
from test_context_actions import context as _context

from voicebar.calculator import keypad_sequence
from voicebar.calculator_feedback import display_number, expected_result, matches_result
from voicebar.executor import app_actions

context = _context


@pytest.mark.parametrize("phrase,expected", [
    ("2+3", "5"), ("-2--3", "1"), ("2.5 times four", "10"),
    ("12 divided by four", "3"), ("zero point one plus zero point two", "0.3"),
])
def test_expected_result_uses_only_closed_sequence(phrase, expected):
    assert expected_result(keypad_sequence(phrase)) == Decimal(expected)


def test_undefined_and_invalid_arithmetic_cannot_confirm_result():
    assert expected_result(keypad_sequence("2/0")) is None
    assert expected_result(("shell", "+", "1")) is None
    assert not matches_result(Decimal("5.1"), Decimal(5))
    assert matches_result(expected_result(keypad_sequence("2/3")), Decimal("0.6666666666666667"))


@pytest.mark.parametrize("name", ["5", "Display is NaN", "Display is Infinity",
    "Display is 1,2", "Display is 1e999", "Display is Cannot divide by zero"])
def test_unrecognized_display_never_confirms(name):
    assert display_number(name) is None


def install_display(context, previous="0", following="5", lose_focus=False):
    result = row("Display is " + previous, "text", "CalculatorResults")
    context.automation.rows.append(result)
    def send(events):
        context.sent.append(events)
        if events[0][1] == 0x0D:
            result["name"] = "Display is " + following
            if lose_focus:
                context.native.focus = 3
    context.input_backend.send = send


def test_matching_readback_reports_observed_result_without_repeating_input(context):
    install_display(context)
    detail = app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert detail == "Calculator shows 5"
    assert context.calculator_verification == {"status": "verified", "display": "5"}
    assert len(context.sent) == 5


@pytest.mark.parametrize("following", ["4", "Cannot divide by zero"])
def test_wrong_or_error_result_is_unverified_and_never_retyped(context, following):
    install_display(context, following=following)
    detail = app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert "not confirmed" in detail or "not observable" in detail
    assert context.calculator_verification["status"] == "unverified"
    assert len(context.sent) == 5


def test_same_old_display_does_not_prove_this_invocation(context):
    install_display(context, previous="5")
    detail = app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert "new calculation is not confirmed" in detail
    assert context.calculator_verification["status"] == "unverified"
    assert len(context.sent) == 5


def test_focus_loss_after_delivery_cannot_confirm_another_window(context):
    install_display(context, lose_focus=True)
    detail = app_actions.app_action(context, action("calculate:2+3", "Calculator"))
    assert "result is not observable" in detail
    assert context.calculator_verification["status"] == "unverified"
    assert len(context.sent) == 5


def test_bounded_readback_wait_observes_late_result_without_retyping(context):
    install_display(context, following="0")
    waits = []
    def wait(predicate, seconds, description):
        waits.append(seconds)
        context.automation.rows[-1]["name"] = "Display is 5"
        assert predicate()
    context.native.wait = wait
    assert app_actions.app_action(context, action("calculate:2+3", "Calculator")) == "Calculator shows 5"
    assert waits == [1.0] and len(context.sent) == 5
