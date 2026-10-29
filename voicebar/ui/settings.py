"""Validated desktop settings, separate from the closed voice action schema."""

import math

from ..catalog import CatalogError, _yaml_file
from ..ptt import parse_hotkey


def load_ui_settings(root, document=None):
    document = _yaml_file(root / "config/settings.yaml") if document is None else document
    supplied = document.get("ui", {})
    if not isinstance(document.get("ptt", {}), dict):
        raise CatalogError("settings.yaml ptt must be a mapping")
    if not isinstance(supplied, dict):
        raise CatalogError("settings.yaml ui must be a mapping")
    result = {"monitor": "primary", "dwell_ms": 450, "idle_ms": 4000,
              "toggle_listening": "ctrl+alt+f9", "toggle_bar": "ctrl+alt+f10",
              "hold_to_talk": document.get("ptt", {}).get("hotkey", "ctrl+alt+f8"),
              "execution_timeout_seconds": 10, **supplied}
    result.setdefault("copilot_key", True)
    result.setdefault("style", "strip")
    allowed = {"monitor", "dwell_ms", "idle_ms", "toggle_listening", "toggle_bar",
               "hold_to_talk", "execution_timeout_seconds", "copilot_key", "style"}
    if set(supplied) - allowed:
        raise CatalogError(f"Unknown ui settings: {sorted(set(supplied) - allowed)}")
    if not isinstance(result["monitor"], str) or not result["monitor"]:
        raise CatalogError("ui monitor must be primary or a Windows display name")
    if type(result["copilot_key"]) is not bool:
        raise CatalogError("ui copilot_key must be true or false")
    if result["style"] not in {"strip", "pill"}:
        raise CatalogError("ui style must be strip or pill")
    for key, low, high in (("dwell_ms", 150, 3000), ("idle_ms", 1000, 60000),
                           ("execution_timeout_seconds", 1, 120)):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or not low <= value <= high):
            raise CatalogError(f"ui {key} must be from {low} to {high}")
    shortcuts = [parse_hotkey(result[key]).text
                 for key in ("toggle_listening", "toggle_bar", "hold_to_talk")]
    if len(set(shortcuts)) != 3:
        raise CatalogError("The three UI hotkeys must be distinct")
    return result
