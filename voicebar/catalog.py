from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .normalization import normalized_words, phrase_key
from .schema import _url as _schema_url


@dataclass
class Catalog:
    apps: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    sites: dict[str, str] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)
    command_aliases: dict[str, list[dict]] = field(default_factory=dict)
    noncommands: set[str] = field(default_factory=set)
    app_actions: dict[str, dict[str, dict]] = field(default_factory=dict)
    active_app: str | None = None
    browser_apps: tuple[str, ...] = ()


class CatalogError(ValueError):
    """A catalog or configuration problem that can be shown without a traceback."""


@dataclass(frozen=True)
class InstalledApp:
    name: str
    app_id: str


@dataclass
class LoadedCatalog:
    catalog: Catalog
    installed_apps: list[InstalledApp]
    site_titles: dict[str, str]
    pinned: list[str]


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loading with duplicate mapping keys rejected, not overwritten."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise CatalogError("mapping keys must be text")
            if key in result:
                raise CatalogError(f"duplicate key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _yaml_file(path: Path):
    try:
        # SafeLoader refuses Python object construction; the subclass adds duplicates.
        return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueSafeLoader)
    except CatalogError as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"Cannot read {path.name}: {exc}") from exc


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be a mapping")
    return value


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label} must be nonempty text")
    return value


def _phrase(value, label: str) -> str:
    # The parser ignores case and spaces for exact phrases. Store readable spacing.
    normalized = " ".join(normalized_words(_text(value, label)))
    if not normalized:
        raise CatalogError(f"{label} must contain a spoken word")
    return normalized


def _phrase_key(value: str) -> str:
    return phrase_key(value)


def _claim_phrase(phrase: str, seen: dict[str, str], label: str) -> None:
    key = _phrase_key(phrase)
    if key in seen:
        raise CatalogError(f"Duplicate phrase {phrase!r} in {label}; also in {seen[key]}")
    seen[key] = label


def _url(value, label: str, *, template: bool = False) -> str:
    value = _text(value, label)
    candidate = value
    if template:
        if value.count("{q}") != 1:
            raise CatalogError(f"{label} must contain exactly one {{q}} placeholder")
        candidate = value.replace("{q}", "voicebar")
    invalid = any(char.isspace() for char in candidate) or bool(
        re.search(r'[\\<>"{}]|%(?![0-9a-fA-F]{2})', candidate)
    )
    # Apply the same host, credentials, controls and port checks as action plans.
    if invalid or not _schema_url(candidate):
        raise CatalogError(f"{label} must be a valid http:// or https:// URL")
    return value


_START_APPS_COMMAND = (
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
    "ConvertTo-Json -InputObject @(Get-StartApps | Select-Object Name, AppID) "
    "-Depth 3 -Compress"
)


def _get_start_apps() -> list:
    """Read Windows Start menu registrations with a fixed PowerShell command."""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _START_APPS_COMMAND],
            shell=False,
            capture_output=True,
            encoding="utf-8-sig",
            errors="strict",
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise CatalogError(f"Get-StartApps failed (exit {exc.returncode}){suffix}") from exc
    except (OSError, UnicodeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot read Get-StartApps output: {exc}") from exc


def _installed_apps(rows) -> list[InstalledApp]:
    if not isinstance(rows, list):
        raise CatalogError("Start menu catalog must be a list of Name/AppID records")
    result = []
    ids = set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise CatalogError(f"Start menu record {number} must be a Name/AppID mapping")
        name = _text(row.get("Name"), f"Start menu record {number} Name")
        app_id = _text(row.get("AppID"), f"Start menu record {number} AppID")
        if app_id in ids:
            raise CatalogError(f"Start menu catalog has duplicate AppID {app_id!r}")
        ids.add(app_id)
        result.append(InstalledApp(name, app_id))
    return result


def _unique_app(target, installed: list[InstalledApp], label: str) -> str:
    target = _text(target, label)
    matches = [app for app in installed if app.name == target]
    if not matches:
        raise CatalogError(f"{label} points to app {target!r}, which is not installed")
    # Parser exact matching ignores case/spaces, so check that ambiguity too.
    equivalent = [app for app in installed if _phrase_key(app.name) == _phrase_key(target)]
    if len(equivalent) != 1:
        raise CatalogError(f"{label} points to ambiguous app name {target!r}")
    return target


def load_catalog(project_root: Path, refresh: bool = False) -> LoadedCatalog:
    """Load cached Windows app identities and validated local YAML configuration."""
    project_root = Path(project_root)
    cache = project_root / ".cache" / "startapps.json"
    if refresh or not cache.exists():
        rows = _get_start_apps()
        installed = _installed_apps(rows)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(cache)
        except OSError as exc:
            raise CatalogError(f"Cannot save Start menu cache: {exc}") from exc
    else:
        try:
            rows = json.loads(cache.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Cannot read Start menu cache; run 'voicebar apps --refresh': {exc}") from exc
        installed = _installed_apps(rows)

    config = project_root / "config"
    seen: dict[str, str] = {}
    installed_names = {_phrase_key(app.name): app.name for app in installed}
    aliases = {}
    for phrase, target in _mapping(_yaml_file(config / "aliases.yaml"), "aliases.yaml").items():
        phrase = _phrase(phrase, "Alias phrase")
        _claim_phrase(phrase, seen, "aliases.yaml")
        target = _unique_app(target, installed, f"Alias {phrase!r}")
        matching_app = installed_names.get(_phrase_key(phrase))
        if matching_app is not None and _phrase_key(matching_app) != _phrase_key(target):
            raise CatalogError(f"Duplicate phrase {phrase!r}: alias would hide installed app {matching_app!r}")
        aliases[phrase] = target

    sites, titles = {}, {}
    for phrase, entry in _mapping(_yaml_file(config / "sites.yaml"), "sites.yaml").items():
        phrase = _phrase(phrase, "Site phrase")
        _claim_phrase(phrase, seen, "sites.yaml")
        if _phrase_key(phrase) in installed_names:
            raise CatalogError(f"Duplicate phrase {phrase!r}: site is hidden by an installed app name")
        if isinstance(entry, dict):
            if set(entry) - {"url", "title"}:
                raise CatalogError(f"Site {phrase!r} accepts only url and optional title")
            url = entry.get("url")
            if "title" in entry:
                titles[phrase] = _text(entry["title"], f"Site {phrase!r} title")
        else:
            url = entry
        sites[phrase] = _url(url, f"Site {phrase!r} URL")

    settings = _mapping(_yaml_file(config / "settings.yaml"), "settings.yaml")
    engines, engine_phrases = {}, {}
    for phrase, url in _mapping(settings.get("engines"), "settings.yaml engines").items():
        phrase = _phrase(phrase, "Search engine phrase")
        if len(phrase.split()) != 1:
            raise CatalogError(f"Search engine {phrase!r} must be a single word")
        _claim_phrase(phrase, engine_phrases, "settings.yaml engines")
        engines[phrase] = _url(url, f"Search engine {phrase!r} URL", template=True)
    if "name" in settings:
        _text(settings["name"], "settings.yaml name")
    if "followup_seconds" in settings:
        seconds = settings["followup_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 3600:
            raise CatalogError("settings.yaml followup_seconds must be a number from 0 to 3600")
    if "google" not in engines:
        raise CatalogError("settings.yaml engines must define 'google', the parser's default search engine")

    pinned = _yaml_file(config / "pinned.yaml")
    if not isinstance(pinned, list):
        raise CatalogError("pinned.yaml must be a list of installed app names")
    pinned_names = []
    for target in pinned:
        name = _unique_app(target, installed, "Pinned entry")
        if name in pinned_names:
            raise CatalogError(f"pinned.yaml has duplicate app {name!r}")
        pinned_names.append(name)

    from .app_actions import AppActionError, load_app_actions
    try:
        app_actions = load_app_actions(config / "apps", [app.name for app in installed])
    except AppActionError as exc:
        raise CatalogError(str(exc)) from exc
    catalog = Catalog([app.name for app in installed], aliases, sites, engines,
                      app_actions=app_actions)
    executor_settings = _mapping(settings.get("executor", {}), "settings.yaml executor")
    browser_apps = executor_settings.get("browser_apps", [])
    if not isinstance(browser_apps, list) or any(not isinstance(app, str) for app in browser_apps):
        raise CatalogError("executor browser_apps must be a list of app names")
    catalog.browser_apps = tuple(app for app in browser_apps if app in catalog.apps)
    learned_path = config / "learned.yaml"
    if learned_path.exists():
        from .schema import validate_plan
        learned = _mapping(_yaml_file(learned_path), "learned.yaml")
        if set(learned) - {"aliases", "non_commands"}:
            raise CatalogError("learned.yaml accepts only aliases and non_commands")
        for phrase, plan in _mapping(learned.get("aliases", {}), "Learned aliases").items():
            key = _phrase(phrase, "Learned alias")
            validate_plan(plan)
            for action in plan:
                if action["action"] == "app_action":
                    raise CatalogError("Learned app_action is forbidden; use a verified config/apps definition")
                for field in ("app", "target"):
                    if field in action and action[field] != "active":
                        _unique_app(action[field], installed, "Learned target")
                if "engine" in action and action["engine"] not in engines:
                    raise CatalogError("Learned search engine is not configured")
                if "site" in action and action["site"] not in sites:
                    raise CatalogError("Learned site is not configured")
                if action["action"] == "open_url" and action["url"] not in sites.values():
                    raise CatalogError("Learned URL must be a configured site")
            if key in catalog.command_aliases:
                raise CatalogError("Duplicate learned alias")
            catalog.command_aliases[key] = plan
        noncommands = learned.get("non_commands", [])
        if not isinstance(noncommands, list):
            raise CatalogError("Learned non_commands must be a list")
        for phrase in noncommands:
            key = _phrase(phrase, "Learned non-command")
            if key in catalog.command_aliases or key in catalog.noncommands:
                raise CatalogError("Conflicting or duplicate learned phrase")
            catalog.noncommands.add(key)
    return LoadedCatalog(
        catalog,
        installed,
        titles,
        pinned_names,
    )
