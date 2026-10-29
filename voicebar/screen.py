"""Resolve a named on-screen control from a fresh local accessibility query."""

from __future__ import annotations

import re

from .parser import _payload, _strip_politeness, tokenize

CONTROL_ROLES = {"button": "button", "link": "hyperlink", "tab": "tabitem",
                 "menuitem": "menuitem", "checkbox": "checkbox", "field": "edit",
                 "radio button": "radiobutton"}


def vision_settings(document):
    """Validate optional local vision without opening a connection."""
    from .catalog import CatalogError
    from .resolver.ollama import OllamaBackend
    config = document.get("vision", {})
    if not isinstance(config, dict) or set(config) - {"enabled", "model", "timeout_ms"}:
        raise CatalogError("vision accepts only enabled, model and timeout_ms")
    enabled = config.get("enabled", False)
    timeout = config.get("timeout_ms", 5000)
    if type(enabled) is not bool or type(timeout) is not int or not 500 <= timeout <= 10000:
        raise CatalogError("vision enabled must be boolean and timeout_ms from 500 to 10000")
    model = config.get("model", "qwen3-vl:2b-instruct")
    port = document.get("resolver", {}).get("port", 11434)
    OllamaBackend(model, port=port)
    return {"enabled": enabled, "model": model, "port": port, "timeout_s": timeout / 1000}


def requested_label(text):
    return requested_control(text)[0]


def requested_control(text):
    tokens = _strip_politeness(tokenize(text))
    words = " ".join(token.norm for token in tokens)
    prefix = re.match(r"^(?:open|show|select|go to|click|tap|choose) (?:on )?(?:the )?", words)
    if prefix is None:
        return None, None
    remainder = tokens[len(prefix.group().split()):]
    role = None
    for spoken, candidate in CONTROL_ROLES.items():
        words = spoken.split()
        if len(remainder) > len(words) and [token.norm for token in remainder[:len(words)]] == words:
            role = candidate
            remainder = remainder[len(words):]
            break
    label = _payload(remainder)
    # UI labels are data. Bound the query and never reinterpret chained or
    # incomplete instructions as separate side effects.
    return (label, role) if label and len(label) <= 200 else (None, None)


def resolve_control(text, root, loaded, *, settings=None, frames=None,
                    generation=0, selector=None, timeout_s=5.0, authorized=lambda: True,
                    on_vision=lambda: None, required_foreground=None):
    label, requested_role = requested_control(text)
    visual_request = label is not None or " ".join(text.lower().split()).rstrip(".?!") in {
        "take a picture", "take a photo",
    }
    if not visual_request:
        return None
    from .executor.app_actions import _automation
    from .executor.common import Context
    from .executor.input import _foreground_guard
    from .executor.uia import ControlMatchError, unique

    reference = " ".join(token.norm for token in tokenize(label or ""))
    if reference in {"this", "that", "it", "this one", "that one"}:
        raise ControlMatchError("Which control should I click? Say its visible label, such as 'click Bookmarks'.", [])

    owned = {required_foreground[0]: required_foreground[1]} if required_foreground else None
    with Context(root, loaded=loaded, persist=False, settings=settings, owned=owned) as ctx:
        if required_foreground is not None:
            ctx.required_foreground = required_foreground
        window = ctx.target("active")
        automation = _automation(ctx)
        target = {"name": label, **({"role": requested_role} if requested_role else {})}
        rows = automation.find(window.hwnd, target) if label else []
        _foreground_guard(ctx, window)
        if not rows:
            if frames is None or selector is None:
                return None
            frame = frames.get(window.hwnd, window.pid, generation)
            if frame is None or not authorized():
                raise ControlMatchError("No current screen image; repeat the command once the window is visible", [])
            snapshot = automation.snapshot(window.hwnd)
            alternatives = label_alternatives(label, snapshot, requested_role)
            if len(alternatives) > 1:
                names = ", ".join(dict.fromkeys(row["name"] for row in alternatives[:4]))
                raise ControlMatchError(
                    f"Which '{label}' control? Visible matches include {names}. Say its full label and role.",
                    alternatives[:8])
            observed = visual_candidates(snapshot)
            if requested_role:
                observed = [row for row in observed if row["role"] == requested_role]
                observed = [dict(row, id=index) for index, row in enumerate(observed, 1)]
            signature = observation_signature(snapshot)
            _foreground_guard(ctx, window)
            # Never truncate a crowded screen into an artificially unique choice.
            if not observed or len(observed) > 96:
                raise ControlMatchError("No bounded set of unique screen controls is available", [])
            frame = frames.get(window.hwnd, window.pid, generation)
            if not authorized() or frame is None:
                raise ControlMatchError("Screen authorization changed; no control was clicked", [])
            on_vision()
            choice = selector.select(text, frame.png, observed, timeout_s=timeout_s)
            _foreground_guard(ctx, window)
            if not authorized() or frames.get(window.hwnd, window.pid, generation) is None:
                raise ControlMatchError("Screen authorization changed; no control was clicked", [])
            if type(choice) is not int or not 1 <= choice <= len(observed):
                outcome = getattr(selector, "last_outcome", "not_sure")
                if outcome in {"timeout", "busy"}:
                    raise ControlMatchError("Screen understanding took too long; try the exact visible label", [])
                if outcome == "backend_error":
                    raise ControlMatchError("Local vision is unavailable; check the local model service", [])
                raise ControlMatchError(
                    f"The screen target is unclear. Which control do you mean by '{label or text}'? Say its visible label and role.",
                    observed[:8])
            # A changed page/menu invalidates the selection. Execution will also
            # requery its unique label/role under the original HWND/PID guard.
            if observation_signature(automation.snapshot(window.hwnd)) != signature:
                raise ControlMatchError("The screen controls changed; repeat the command", [])
            selected = observed[choice - 1]
            _foreground_guard(ctx, window)
            return [_control_action(selected)], (window.hwnd, window.pid)
        if len(rows) > 1:
            raise ControlMatchError(
                f"Which '{label}' control? I found {len(rows)} matches; specify its role or a distinct label.",
                rows[:8])
        selected = unique(rows, target)
        if not selected.get("enabled", False):
            raise ControlMatchError("The named control is disabled", [selected])
        # Use a role as well as its observed label. Execution performs another
        # unique lookup and validates the original HWND/PID before clicking.
        role = selected["role"].replace(" ", "").lower()
        if role not in {"button", "hyperlink", "tabitem", "menuitem", "edit", "checkbox", "radiobutton"}:
            return None
        return [_control_action(selected)], (window.hwnd, window.pid)


def _control_action(selected):
    role = selected["role"].replace(" ", "").lower()
    return {"action": "app_action", "app": "active",
            "name": f"click_role:{role}:{selected['name']}"}


def label_alternatives(label, rows, requested_role=None):
    """Refuse literal label ambiguity; partial matches never authorize clicks.

    Keep duplicate controls here even though they cannot become uniquely
    addressable model options. Do not discard qualifiers from the request.
    """
    from .executor.app_actions import CLICK_ROLES
    words = set(re.findall(r"\w+", (label or "").casefold()))
    if not words:
        return []
    return [row for row in rows
            if row.get("enabled") and row.get("clickable")
            and (role := row.get("role", "").replace(" ", "").lower()) in CLICK_ROLES
            and (requested_role is None or requested_role == role)
            and words <= set(re.findall(r"\w+", row.get("name", "").casefold()))]


def visual_candidates(rows):
    """Only observed, actionable, uniquely named controls become model options."""
    from .executor.app_actions import CLICK_ROLES
    from .executor.uia import _same
    counts = {}
    for row in rows:
        key = (_same(row.get("name", "")), _same(row.get("role", "")))
        counts[key] = counts.get(key, 0) + 1
    result = []
    for row in rows:
        name, role = row.get("name", ""), row.get("role", "").replace(" ", "").lower()
        if (name.strip() and len(name) <= 200 and role in CLICK_ROLES
                and row.get("enabled") and row.get("clickable")
                and counts[(_same(name), _same(row.get("role", "")))] == 1):
            result.append({"id": len(result) + 1, "name": name, "role": role})
    return result


def observation_signature(rows):
    """Include page text and control identity, not just clickable labels."""
    return tuple(tuple(row.get(key) for key in (
        "name", "role", "automation_id", "enabled", "clickable", "pid", "hwnd", "depth"))
                 for row in rows)
