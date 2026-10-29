"""Validate plans against the closed action set in CLAUDE.md.

Validation performs no I/O and does not execute an action. Function-key tokens
follow the parser's numeric spelling (including ``press f 123``); support for a
physical key code belongs to the future executor, not this shape validator.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


class SchemaError(ValueError):
    """An action or plan does not conform to the closed action set."""


_FIELDS = {
    "open_app": {"action", "app"},
    "switch_app": {"action", "app"},
    "close_app": {"action", "app"},
    "window": {"action", "op", "target", "monitor"},
    "open_url": {"action", "url"},
    "web_search": {"action", "engine", "query"},
    "tab": {"action", "op", "site"},
    "nav": {"action", "op"},
    "type_text": {"action", "text", "submit"},
    "press": {"action", "keys"},
    "scroll": {"action", "dir", "amount"},
    "volume": {"action", "op"},
    "media": {"action", "op"},
    "mode": {"action", "op"},
    "app_action": {"action", "app", "name"},
}

_OPS = {
    "window": {
        "maximize", "minimize", "restore", "close", "snap_left", "snap_right",
        "move_monitor",
    },
    "tab": {"new", "close", "next", "prev", "reopen"},
    "nav": {"back", "forward", "reload"},
    "volume": {"up", "down", "mute", "unmute"},
    "media": {"play_pause", "next", "prev"},
    "mode": {"sleep", "wake", "dictation_on", "dictation_off"},
}

_NAMED_KEYS = {
    "ctrl", "shift", "alt", "win", "enter", "esc", "tab", "space",
    "backspace", "delete", "up", "down", "left", "right", "home", "end",
    "pageup", "pagedown",
}
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\]")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def _string(action: dict, field: str) -> str:
    value = action.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{action['action']}: '{field}' must be a nonempty string")
    return value


def _choice(action: dict, field: str, choices: set[str]) -> str:
    value = _string(action, field)
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise SchemaError(f"{action['action']}: '{field}' must be one of: {allowed}")
    return value


def _url(value: str) -> bool:
    """Require HTTP(S), a valid host, and an optional usable nonzero port.

    Embedded credentials, whitespace, controls, and backslashes are rejected.
    """
    if _URL_FORBIDDEN.search(value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
        # Accessing port also validates its spelling and range in Python 3.12.
        port = parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.netloc.endswith(":")
            or port == 0
        ):
            return False
        if ":" in host:
            # urlsplit checks the IPv6 syntax of bracketed hosts itself.
            return parts.netloc.startswith("[")
        host = host.removesuffix(".")
        ascii_host = host.encode("idna").decode("ascii")
        return len(ascii_host) <= 253 and all(
            _HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")
        )
    except (ValueError, UnicodeError):
        return False


def _keys(value: str) -> bool:
    return all(
        key in _NAMED_KEYS
        or re.fullmatch(r"[a-z0-9]", key) is not None
        or (key.startswith("f") and key[1:].isdigit())
        for key in value.split("+")
    )


def validate_action(action: object) -> dict:
    """Return the original action dict, or raise SchemaError without changing it."""
    if not isinstance(action, dict):
        raise SchemaError("An action must be a dict")
    name = action.get("action")
    if not isinstance(name, str) or name not in _FIELDS:
        raise SchemaError("Unknown action; expected an action from CLAUDE.md")
    if any(not isinstance(field, str) for field in action):
        raise SchemaError(f"{name}: field names must be strings")
    extra = action.keys() - _FIELDS[name]
    if extra:
        raise SchemaError(f"{name}: unexpected field(s): {', '.join(sorted(extra))}")

    if name in {"open_app", "switch_app", "close_app", "app_action"}:
        _string(action, "app")
        if name == "app_action":
            _string(action, "name")
    elif name in _OPS:
        op = _choice(action, "op", _OPS[name])
        if name == "window":
            _string(action, "target")
            if op == "move_monitor":
                _choice(action, "monitor", {"next", "prev", "left", "right", "primary"})
            elif "monitor" in action:
                raise SchemaError("window: 'monitor' is only allowed for move_monitor")
        elif name == "tab" and "site" in action:
            if op != "close":
                raise SchemaError("tab: 'site' is only allowed for close")
            _string(action, "site")
    elif name == "open_url":
        if not _url(_string(action, "url")):
            raise SchemaError("open_url: 'url' must be an absolute HTTP(S) URL with a valid host")
    elif name == "web_search":
        _string(action, "engine")
        _string(action, "query")
    elif name == "type_text":
        _string(action, "text")
        if type(action.get("submit")) is not bool:
            raise SchemaError("type_text: 'submit' must be a bool")
    elif name == "press":
        if not _keys(_string(action, "keys")):
            raise SchemaError("press: 'keys' must contain recognized keys separated by '+'")
    elif name == "scroll":
        direction = _choice(action, "dir", {"up", "down", "top", "bottom"})
        if direction in {"up", "down"}:
            _choice(action, "amount", {"small", "page", "large"})
        elif "amount" in action:
            raise SchemaError("scroll: 'amount' is only allowed for up and down")
    return action


def validate_plan(plan: object) -> list[dict]:
    """Require a nonempty action list and return it unchanged after validation."""
    if not isinstance(plan, list) or not plan:
        raise SchemaError("A plan must be a nonempty list of actions")
    for index, action in enumerate(plan):
        try:
            validate_action(action)
        except SchemaError as exc:
            raise SchemaError(f"Action {index + 1}: {exc}") from exc
    return plan
