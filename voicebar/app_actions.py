"""Strict local definitions for the Stage 7, closed app-action step set.

The loaded registry contains plain data. It has no expression expansion, command
runner, or transcript input; the parser only selects an app and an action name.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import yaml

from .normalization import normalized_words, phrase_key
from .schema import SchemaError, validate_action


class AppActionError(ValueError):
    """An app definition is invalid and must not be made executable."""


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise AppActionError("mapping keys must be text")
            if key in result:
                raise AppActionError(f"duplicate key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_SELECTOR_FIELDS = {"name", "role", "automation_id"}
_TEMPLATE = re.compile(r"[{}]|\$[A-Za-z_(]|%[^%\r\n]+%|<%|%>|<<|>>")
_ACTION_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _command_host(app):
    """Refuse observed shell/interpreter app families, including developer prompts."""
    words = normalized_words(app)
    name = " ".join(words)
    return ("powershell" in words or "terminal" in words
            or " command prompt " in f" {name} "
            or name in {"cmd", "cmd.exe", "pwsh", "pwsh.exe", "bash", "bash.exe",
                        "git bash", "anaconda prompt", "python", "python.exe", "node.js"}
            or words[:1] == ("idle",)
            or re.fullmatch(r"python \d+(?:\.\d+)*(?: \d+-bit)?", name) is not None)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AppActionError(f"{label} must be nonempty text")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise AppActionError(f"{label} contains a control character")
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise AppActionError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise AppActionError(f"{label} mapping keys must be text")
    return value


def _selector(value, label, *, waiting=False):
    value = _mapping(value, label)
    fields = _SELECTOR_FIELDS | ({"timeout"} if waiting else set())
    if set(value) - fields:
        raise AppActionError(f"{label} accepts only {', '.join(sorted(fields))}")
    if not set(value) & _SELECTOR_FIELDS:
        raise AppActionError(f"{label} needs name, role or automation_id")
    result = {key: _text(text, f"{label} {key}") for key, text in value.items()
              if key in _SELECTOR_FIELDS}
    if waiting:
        seconds = value.get("timeout")
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= 30):
            raise AppActionError(f"{label} timeout must be a finite number greater than 0 and at most 30")
        result["timeout"] = seconds
    return result


def _shortcut(value, label):
    value = _text(value, label)
    try:
        validate_action({"action": "press", "keys": value})
    except SchemaError as exc:
        raise AppActionError(f"{label}: {exc}") from exc
    # This is the existing native input resolver, not a second spelling of its
    # supported keys. Calling it creates data only; no Win32 object is created.
    from .executor.common import ExecutionError
    from .executor.input import key_events
    try:
        key_events(value)
    except ExecutionError as exc:
        raise AppActionError(f"{label}: {exc}") from exc
    chord = set(value.split("+"))
    if "win" in chord or chord == {"ctrl", "esc"}:
        raise AppActionError(f"{label}: system command launch shortcuts are forbidden")
    return value


def validate_definition(value, installed_names: list[str], *, label="App definition"):
    """Return a copied canonical app/name registry entry, without any I/O."""
    value = _mapping(value, label)
    if set(value) != {"app", "actions"}:
        raise AppActionError(f"{label} requires only app and actions")
    app = _text(value["app"], f"{label} app")
    if phrase_key(app) == "active":
        raise AppActionError(f"{label}: app 'active' is reserved for generic click")
    if app not in installed_names:
        raise AppActionError(f"{label}: app {app!r} is not installed under that exact name")
    if sum(phrase_key(candidate) == phrase_key(app) for candidate in installed_names) != 1:
        raise AppActionError(f"{label}: app {app!r} has an ambiguous installed name")
    if _command_host(app):
        raise AppActionError(f"{label}: shell/command-host app {app!r} cannot have app actions")
    actions = _mapping(value["actions"], f"{label} actions")
    if not actions:
        raise AppActionError(f"{label} actions must be nonempty")
    result, phrases = {}, {}
    for name, definition in actions.items():
        if isinstance(name, str) and name.startswith(("click:", "switch_tab:")):
            raise AppActionError(f"{label}: action name {name!r} is reserved for a code-generated action")
        if not isinstance(name, str) or not _ACTION_NAME.fullmatch(name):
            raise AppActionError(f"{label}: action names must use lowercase letters, numbers and underscores")
        action_label = f"{label} action {name!r}"
        definition = _mapping(definition, action_label)
        if set(definition) - {"say", "do", "then"} or not {"say", "do"} <= set(definition):
            raise AppActionError(f"{action_label} requires say and do, and accepts only optional then")
        spoken = definition["say"]
        if not isinstance(spoken, list) or not spoken:
            raise AppActionError(f"{action_label} say must be a nonempty list of phrases")
        normalized = []
        for phrase in spoken:
            phrase = " ".join(normalized_words(_text(phrase, f"{action_label} say phrase")))
            if not phrase:
                raise AppActionError(f"{action_label} say phrase must contain a spoken word")
            key = phrase_key(phrase)
            if key in phrases:
                raise AppActionError(f"{action_label}: duplicate phrase {phrase!r}; also in {phrases[key]!r}")
            phrases[key] = name
            normalized.append(phrase)
        steps = definition["do"]
        if not isinstance(steps, list) or not steps:
            raise AppActionError(f"{action_label} do must be a nonempty ordered list of steps")
        validated = []
        for number, step in enumerate(steps, 1):
            step_label = f"{action_label} step {number}"
            step = _mapping(step, step_label)
            if len(step) != 1 or not set(step) <= {"keys", "click", "type", "wait", "mcp"}:
                raise AppActionError(f"{step_label} must contain exactly one of keys, click, type, wait or mcp")
            kind, payload = next(iter(step.items()))
            if kind == "keys":
                payload = _shortcut(payload, step_label)
            elif kind in {"click", "wait"}:
                payload = _selector(payload, step_label, waiting=kind == "wait")
            elif kind == "mcp":
                if app != "Autodesk Fusion":
                    raise AppActionError(f"{step_label}: mcp steps are only permitted for Autodesk Fusion")
                payload = _mapping(payload, step_label)
                if set(payload) != {"command"}:
                    raise AppActionError(f"{step_label}: mcp accepts only a fixed command name")
                from .fusion_mcp import FusionMCPError, validate_command
                try:
                    command = validate_command(payload["command"])
                except FusionMCPError as exc:
                    raise AppActionError(f"{step_label}: {exc}") from exc
                payload = {"command": command}
            else:
                payload = _text(payload, f"{step_label} type")
                if _TEMPLATE.search(payload):
                    raise AppActionError(f"{step_label} type must be fixed text without template or transcript placeholders")
            validated.append({kind: payload})
        result[name] = {"say": normalized, "do": validated}
        if "then" in definition:
            if definition["then"] != "sleep":
                raise AppActionError(f"{action_label} then accepts only sleep")
            result[name]["then"] = "sleep"
    return app, result


def load_app_actions(directory: Path, installed_names: list[str]) -> dict[str, dict[str, dict]]:
    """Load every YAML app definition; a single invalid file rejects the load."""
    directory = Path(directory)
    registry = {}
    try:
        if not directory.exists():
            return {}
        if not directory.is_dir():
            raise AppActionError(f"{directory.name} must be a directory")
        paths = sorted(directory.glob("*.yaml"))
        for path in paths:
            try:
                value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueSafeLoader)
                app, actions = validate_definition(value, installed_names, label=path.name)
            except AppActionError as exc:
                raise AppActionError(f"{path.name}: {exc}") from exc
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise AppActionError(f"Cannot read {path.name}: {exc}") from exc
            if app in registry:
                raise AppActionError(f"{path.name}: duplicate app definition for {app!r}")
            registry[app] = actions
    except OSError as exc:
        raise AppActionError(f"Cannot read {directory.name}: {exc}") from exc
    return registry
