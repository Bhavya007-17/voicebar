"""Foreground-bound execution of configured app actions and exact UIA clicks."""
from __future__ import annotations

from .apps import _activate, _remember
from .common import Context, ExecutionError
from .input import (
    _foreground_guard,
    _send,
    key_event,
    key_events,
    press_window,
    unicode_events,
)
from .uia import Automation, ControlMatchError, ControlTreeChanged, unique

CLICK_ROLES = frozenset({"button", "hyperlink", "tabitem", "menuitem", "edit", "checkbox", "radiobutton"})


def foreground_app(root, loaded, *, settings=None):
    """Read current app identity; no window activation and no runtime writes."""
    if not loaded.catalog.apps:
        return None
    with Context(root, loaded=loaded, persist=False, settings=settings) as ctx:
        hwnd = ctx.native.foreground()
        windows = [window for window in ctx.native.windows() if window.hwnd == hwnd]
        if len(windows) != 1:
            return None
        window = windows[0]
        name = ctx.apps.name_for_window(window)
        if (ctx.native.foreground() != hwnd
                or not ctx.native.is_window(hwnd, window.pid)):
            return None
        return name


def _automation(ctx):
    if getattr(ctx, "automation", None) is None:
        ctx.automation = Automation()
    return ctx.automation


def definition(ctx, action):
    app, name = action["app"], action["name"]
    if app == "active" and name.startswith("click:") and name[6:].strip():
        return None
    if app == "active" and name.startswith("click_role:"):
        parts = name.split(":", 2)
        if len(parts) == 3 and parts[1] in CLICK_ROLES and parts[2].strip():
            return None
    if app in {"active", "Google Chrome"} and name.startswith("switch_tab:") and name[11:].strip():
        return None
    if app == "active" and name == "browser_window:new":
        return None
    if app == "Calculator" and name == "clear":
        return None
    if app == "Calculator" and name.startswith("calculate:"):
        from ..calculator import keypad_sequence
        if keypad_sequence(name[10:]) is None:
            raise ExecutionError("Calculator needs one complete arithmetic expression; no action ran")
        return None
    try:
        return ctx.loaded.catalog.app_actions[app][name]
    except (AttributeError, KeyError) as error:
        raise ExecutionError(f"Unknown configured action {name!r} in {app!r}") from error


def _calculate(ctx, expression):
    from ..calculator import keypad_sequence
    from ..calculator_feedback import display_number, expected_result, matches_result
    sequence = keypad_sequence(expression)
    if sequence is None:
        raise ExecutionError("Calculator needs one complete arithmetic expression; no action ran")
    # Never activate a remembered Calculator over the user's current app.
    window = ctx.target()
    if not ctx.apps.matches(window, "Calculator"):
        raise ExecutionError("Calculator is no longer foreground; calculation refused")
    _foreground_guard(ctx, window)
    # These IDs and the Standard-mode label were observed in our owned Stage 7
    # Calculator probe. Converter/programmer/search surfaces are not authorized.
    automation = _automation(ctx)
    rows = automation.find(window.hwnd, {"automation_id": "Header", "role": "text"})
    mode = unique(rows, {"automation_id": "Header", "role": "text"})
    if " ".join(mode["name"].split()).casefold() not in {
        "standard calculator mode", "scientific calculator mode",
    } or not mode["enabled"]:
        raise ExecutionError("Calculator must be in Standard or Scientific mode; no calculation ran")
    expected = expected_result(sequence)

    def read_display():
        _foreground_guard(ctx, window)
        results = automation.find(window.hwnd, {"automation_id": "CalculatorResults", "role": "text"})
        _foreground_guard(ctx, window)
        if len(results) != 1:
            return None
        return display_number(results[0].get("name"))

    try:
        previous = read_display()
    except (RuntimeError, OSError, ValueError):
        previous = None
    # Windows virtual-key values verified against Microsoft's current Winuser.h
    # reference. Fixed keypad operators avoid keyboard-layout punctuation keys.
    operators = {"*": 0x6A, "+": 0x6B, "-": 0x6D, ".": 0x6E, "/": 0x6F}
    _send(ctx, window, key_events("esc"))
    for token in sequence:
        if token in operators:
            vk = operators[token]
            events = [key_event(vk), key_event(vk, True)]
        else:
            events = key_events("f9" if token == "negate" else token)
        _send(ctx, window, events)
    _send(ctx, window, key_events("enter"))
    _remember(ctx, "Calculator")
    fallback = "Calculator expression and equals inserted; application result is not observable"
    ctx.calculator_verification = {"status": "unverified"}
    try:
        observed = read_display()
        if observed is None:
            return fallback
        if previous is not None and matches_result(expected, previous[1]) and observed[1] == previous[1]:
            # Equal old/new displays do not prove this invocation took effect.
            return f"Calculator shows {observed[0]}; new calculation is not confirmed"

        def result_matches():
            nonlocal observed
            observed = read_display()
            return observed is not None and matches_result(expected, observed[1])

        if expected is not None and not matches_result(expected, observed[1]):
            wait = getattr(ctx.native, "wait", None)
            if wait is not None:
                wait(result_matches, 1.0, "Calculator's expected result")
        if observed is not None and matches_result(expected, observed[1]):
            ctx.calculator_verification = {"status": "verified", "display": observed[0]}
            return f"Calculator shows {observed[0]}"
    except (RuntimeError, OSError, ValueError):
        # Input was already delivered. An unavailable result must never cause
        # an automatic replay or turn successful delivery into a new mutation.
        return fallback
    return "Calculator expression and equals inserted; result is not confirmed"


def _clear_calculator(ctx):
    from ..calculator_feedback import display_number

    window = ctx.target()
    if not ctx.apps.matches(window, "Calculator"):
        raise ExecutionError("Calculator is no longer foreground; clear refused")
    _foreground_guard(ctx, window)
    automation = _automation(ctx)
    mode = unique(automation.find(window.hwnd, {"automation_id": "Header", "role": "text"}),
                  {"automation_id": "Header", "role": "text"})
    _foreground_guard(ctx, window)
    if " ".join(mode["name"].split()).casefold() not in {
        "standard calculator mode", "scientific calculator mode",
    } or not mode["enabled"]:
        raise ExecutionError("Calculator must be in Standard or Scientific mode; no clear ran")

    def guard():
        _foreground_guard(ctx, window)
        ctx.begin()

    # Observed in the owned Stage 7 Calculator probe. The app, mode, role,
    # label, and stable ID must all match; a different app's Clear is never used.
    automation.click(window.hwnd, {"name": "Clear", "role": "button", "automation_id": "clearButton"}, guard)
    _remember(ctx, "Calculator")
    ctx.calculator_verification = {"status": "unverified"}
    observed = None

    def cleared():
        nonlocal observed
        _foreground_guard(ctx, window)
        rows = automation.find(window.hwnd, {"automation_id": "CalculatorResults", "role": "text"})
        _foreground_guard(ctx, window)
        observed = display_number(rows[0].get("name")) if len(rows) == 1 else None
        return observed is not None and observed[1] == 0

    try:
        if cleared():
            ctx.calculator_verification = {"status": "verified", "display": observed[0]}
            return "Calculator shows 0"
        if observed is not None and (wait := getattr(ctx.native, "wait", None)) is not None:
            wait(cleared, 1.0, "Calculator's cleared display")
            if observed is not None and observed[1] == 0:
                ctx.calculator_verification = {"status": "verified", "display": observed[0]}
                return "Calculator shows 0"
    except (RuntimeError, OSError, ValueError):
        # The click was delivered once. Failure to observe it cannot replay it.
        return "Calculator Clear clicked; application result is not observable"
    return "Calculator Clear clicked; result is not confirmed"


def app_action(ctx, action):
    configured = definition(ctx, action)
    app, name = action["app"], action["name"]
    if app == "Calculator" and name.startswith("calculate:"):
        return _calculate(ctx, name[10:])
    if app == "Calculator" and name == "clear":
        return _clear_calculator(ctx)
    window = ctx.target(app)
    if app != "active":
        _activate(ctx, window)

    def guard():
        _foreground_guard(ctx, window)
        ctx.begin()

    if app == "active" and name == "browser_window:new":
        if not ctx.apps.browser(window):
            raise ExecutionError("Foreground target is not a verified browser window")
        press_window(ctx, window, "ctrl+n")
        return "New browser window shortcut inserted; window creation is not observable"
    if app == "active" and name.startswith(("click:", "click_role:")):
        automation = _automation(ctx)
        selector = {"name": name[6:]}
        if name.startswith("click_role:"):
            _, role, label = name.split(":", 2)
            selector = {"name": label, "role": role}
        # UIA's exact matcher normalizes case and whitespace. Punctuation stays
        # meaningful ("Save" and "Save..."); duplicate matches remain refusal.
        control = automation.click(window.hwnd, selector, guard)
        return f"Clicked {control['name']!r} through UI Automation"
    if name.startswith("switch_tab:"):
        if app == "active" and not ctx.apps.browser(window):
            raise ExecutionError("Foreground target is not a verified browser window")
        automation = _automation(ctx)
        title = name[11:]
        rows = automation.find(window.hwnd, {"role": "tabitem"})
        # Use the actual tab title. Fuzzy/substrings must never switch an
        # unrelated tab. Browser tab search is available as its own fixed action.
        tabs = [row for row in rows if row["role"].replace(" ", "") == "tabitem"]
        target = unique(tabs, {"name": title})
        automation.click(window.hwnd, {"name": target["name"], "role": target["role"]}, guard)
        if app != "active":
            _remember(ctx, app)
        return f"Selected browser tab {title!r} through UI Automation"

    for step in configured["do"]:
        _foreground_guard(ctx, window)
        kind, value = next(iter(step.items()))
        if kind == "keys":
            press_window(ctx, window, value)
        elif kind == "click":
            _automation(ctx).click(window.hwnd, value, guard)
        elif kind == "type":
            # Fixed local definition data only. No transcript expansion or
            # clipboard mutation; every batch rechecks this same HWND/PID.
            for char in value:
                _send(ctx, window, unicode_events(char))
        elif kind == "wait":
            def exists(value=value):
                _foreground_guard(ctx, window)
                try:
                    rows = _automation(ctx).find(window.hwnd, value)
                except ControlTreeChanged:
                    return False
                _foreground_guard(ctx, window)
                matches = [row for row in rows
                           if all(" ".join(str(row.get(key, "")).split()).casefold()
                                  == " ".join(text.split()).casefold()
                                  for key, text in value.items() if key != "timeout")]
                if len(matches) > 1:
                    raise ControlMatchError("Wait target is ambiguous", matches[:30])
                return matches
            ctx.native.wait(exists, value["timeout"], f"UI control {value}")
        elif kind == "mcp":
            from ..fusion_mcp import call_command
            call_command(value["command"], guard=guard, cancel=getattr(ctx, "cancel", None))
        else:
            raise ExecutionError(f"Unsupported app-action step {kind!r}")
    _remember(ctx, app)
    return f"Completed configured steps for {name!r} in {app}; verify application state"
