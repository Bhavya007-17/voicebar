"""Review local decision logs and save explicitly accepted command aliases.

This module never calls a model or an executor. Log reading has byte and row
bounds; learned plans are validated again before an atomic local YAML write.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .catalog import Catalog, CatalogError, _UniqueSafeLoader
from .normalization import normalized_words
from .schema import SchemaError, validate_plan

MAX_LOG_FILES = 32
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_ENTRIES = 100
MAX_PHRASE_CHARS = 4096
MAX_LEARNED_BYTES = 1024 * 1024
MAX_PLAN_ACTIONS = 8
REPEAT_THRESHOLD = 3


class LearningError(ValueError):
    """A learning choice cannot be safely stored without changing an old choice."""


@dataclass(frozen=True)
class LearningEntry:
    phrase: str
    count: int
    reason: str
    plan: list[dict] | None = None


@dataclass
class _Group:
    phrase: str
    reason: str
    count: int = 0
    successes: Counter = field(default_factory=Counter)
    plans: dict[str, list[dict]] = field(default_factory=dict)


def _key(phrase: str) -> str:
    if not isinstance(phrase, str) or len(phrase) > MAX_PHRASE_CHARS:
        raise LearningError("Phrase must be text of at most 4096 characters")
    key = " ".join(normalized_words(phrase))
    if not key or any(ord(char) < 32 and not char.isspace() for char in phrase):
        raise LearningError("Phrase must contain spoken words without control characters")
    return key


def _without_name(text: str, names: set[str]) -> str:
    # Keep raw spacing, punctuation and case after the spoken name. Unlike the
    # gate's token rejoin, this must preserve a future type/search payload.
    for token in re.finditer(r"\S+", text):
        words = normalized_words(token.group())
        if not words:
            continue
        word = words[0]
        if word in {"hey", "ok", "okay"}:
            continue
        if word in names:
            return text[token.end():].strip()
        break
    return text.strip()


def _recent_rows(root: Path):
    try:
        files = []
        for path in (root / "logs").glob("*.jsonl"):
            if path.is_symlink():
                continue
            try:
                files.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0], reverse=True)
    except OSError:
        return
    remaining = MAX_TOTAL_BYTES
    for _, path in files[:MAX_LOG_FILES]:
        if remaining <= 0:
            return
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                length = min(size, MAX_FILE_BYTES, remaining)
                start = size - length
                handle.seek(start)
                data = handle.read(length)
            remaining -= len(data)
            if start:
                # The byte boundary can be in a line or a UTF-8 sequence.
                # Discard that incomplete row rather than guessing its start.
                _, _, data = data.partition(b"\n")
            for line in reversed(data.splitlines()):
                if not line or len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(row, dict):
                    yield row
        except OSError:
            continue


def _successful_tier2(row: dict) -> bool:
    results = row.get("results")
    return (
        row.get("tier") in ("tier2", "tier_2", 2)
        and row.get("reason") == "run"
        and row.get("dry_run") is False
        and row.get("executed") is True
        and isinstance(results, list)
        and bool(results)
        and all(isinstance(result, dict) and result.get("ok") is True for result in results)
        and isinstance(row.get("plan"), list)
        and len(results) == len(row["plan"])
    )


def recent_entries(root: Path, names) -> list[LearningEntry]:
    """Newest unresolved phrases and unambiguous repeated tier-2 successes.

    Suggestions require three complete real executions of the same plan. A
    contradictory success suppresses the suggestion; dry runs never teach.
    Names are stripped only when actually present at the start of the log text.
    """
    wake_names = {name.lower() for name in names if isinstance(name, str)}
    groups: dict[str, _Group] = {}
    for row in _recent_rows(Path(root)):
        text = row.get("text")
        if not isinstance(text, str) or len(text) > MAX_PHRASE_CHARS:
            continue
        phrase = _without_name(text, wake_names)
        try:
            key = _key(phrase)
        except LearningError:
            continue
        unresolved = row.get("reason") in ("unresolved", "not sure", "not_sure")
        unresolved = unresolved or row.get("state") == "not sure"
        success = _successful_tier2(row)
        if not unresolved and not success:
            continue
        reason = "not sure" if row.get("state") == "not sure" else row.get("reason", "unresolved")
        group = groups.setdefault(key, _Group(phrase, reason))
        group.count += 1
        if unresolved and group.reason == "run":
            group.reason = reason
        if success:
            try:
                plan = validate_plan(row["plan"])
                if len(plan) > MAX_PLAN_ACTIONS:
                    continue
                _payloads_from_phrase(phrase, plan)
                identity = json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except (SchemaError, LearningError, ValueError, RecursionError):
                continue
            group.successes[identity] += 1
            group.plans[identity] = plan
    entries = []
    for group in groups.values():
        plan = None
        if len(group.successes) == 1:
            identity, count = next(iter(group.successes.items()))
            if count >= REPEAT_THRESHOLD:
                plan = group.plans[identity]
                try:
                    _payloads_from_phrase(group.phrase, plan)
                except LearningError:
                    plan = None
        if plan is not None:
            entries.append(LearningEntry(group.phrase, group.count, "recurring tier 2", deepcopy(plan)))
        elif group.reason != "run":
            entries.append(LearningEntry(group.phrase, group.count, group.reason))
    return entries[:MAX_ENTRIES]


def _payloads_from_phrase(phrase: str, plan: list[dict]) -> None:
    for action in plan:
        field = {"type_text": "text", "web_search": "query"}.get(action["action"])
        if field and action[field] not in phrase:
            raise LearningError(f"{field} must be an exact raw span of the utterance")


def _checked_plan(phrase: str, plan, catalog: Catalog) -> list[dict]:
    try:
        validate_plan(plan)
    except SchemaError as exc:
        raise LearningError(str(exc)) from exc
    if len(plan) > MAX_PLAN_ACTIONS:
        raise LearningError("A learned alias may contain at most eight actions")
    _payloads_from_phrase(phrase, plan)
    if any(action["action"] in {"type_text", "web_search"} for action in plan):
        from .resolver.candidates import build_request

        request = build_request(phrase, catalog)
        if request is None:
            raise LearningError("Phrase is outside the resolver's transcript limits")
        candidates = [candidate.action for candidate in request.candidates]
        if any(action not in candidates for action in plan
               if action["action"] in {"type_text", "web_search"}):
            raise LearningError("Typed text and queries must match code-built transcript spans")
    for action in plan:
        kind = action["action"]
        app = action.get("app")
        if kind == "app_action":
            raise LearningError("app_action aliases require Stage 7")
        if app is not None and catalog.apps.count(app) != 1:
            raise LearningError("App must be an actual unambiguous catalog entry")
        if (kind == "window" and action["target"] != "active"
                and catalog.apps.count(action["target"]) != 1):
            raise LearningError("Window target must be an actual catalog entry")
        if kind == "open_url" and action["url"] not in catalog.sites.values():
            raise LearningError("URL must be an actual catalog site")
        if kind == "web_search" and action["engine"] not in catalog.engines:
            raise LearningError("Engine must be an actual catalog entry")
        if kind == "tab" and "site" in action and action["site"] not in catalog.sites:
            raise LearningError("Site must be an actual catalog entry")
    return deepcopy(plan)


def _destination(root: Path) -> Path:
    root = Path(root).resolve()
    path = root / "config" / "learned.yaml"
    if not path.resolve().is_relative_to(root):
        raise LearningError("learned.yaml must stay inside the project")
    return path


def _document(path: Path) -> dict:
    if not path.exists():
        return {"aliases": {}, "non_commands": []}
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_LEARNED_BYTES + 1)
        if len(data) > MAX_LEARNED_BYTES:
            raise LearningError("learned.yaml exceeds the one MiB learning limit")
        document = yaml.load(data.decode("utf-8-sig"), Loader=_UniqueSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError, CatalogError, RecursionError) as exc:
        raise LearningError(f"Cannot read learned.yaml: {exc}") from exc
    if not isinstance(document, dict) or set(document) - {"aliases", "non_commands"}:
        raise LearningError("learned.yaml accepts only aliases and non_commands")
    aliases = document.get("aliases", {})
    noncommands = document.get("non_commands", [])
    if not isinstance(aliases, dict) or not isinstance(noncommands, list):
        raise LearningError("learned.yaml aliases must be a mapping and non_commands a list")
    normalized = {}
    for phrase, plan in aliases.items():
        key = _key(phrase)
        if key in normalized:
            raise LearningError("learned.yaml has duplicate normalized alias phrases")
        try:
            validate_plan(plan)
        except SchemaError as exc:
            raise LearningError(f"Invalid learned alias: {exc}") from exc
        normalized[key] = plan
    negatives = [_key(phrase) for phrase in noncommands]
    if len(negatives) != len(set(negatives)) or set(negatives) & normalized.keys():
        raise LearningError("learned.yaml has contradictory or duplicate phrases")
    return {"aliases": normalized, "non_commands": negatives}


def _write(path: Path, document: dict) -> Path:
    serialized = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    if len(serialized.encode("utf-8")) > MAX_LEARNED_BYTES:
        raise LearningError("learned.yaml would exceed the one MiB learning limit")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".learned-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise LearningError(f"Cannot save learned.yaml: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def save_alias(root: Path, phrase: str, plan, catalog: Catalog) -> Path:
    """Save one user-selected plan; refuse to replace contradictory decisions."""
    key = _key(phrase)
    checked = _checked_plan(phrase, plan, catalog)
    path = _destination(root)
    document = _document(path)
    if key in document["non_commands"]:
        raise LearningError("Phrase is already marked as not a command")
    previous = document["aliases"].get(key)
    if previous is not None:
        if previous != checked:
            raise LearningError("Phrase already has a different learned alias")
        return path
    document["aliases"][key] = checked
    return _write(path, document)


def mark_not_command(root: Path, phrase: str) -> Path:
    """Save an explicitly rejected phrase; refuse to overwrite a learned alias."""
    key = _key(phrase)
    path = _destination(root)
    document = _document(path)
    if key in document["aliases"]:
        raise LearningError("Phrase already has a learned alias")
    if key in document["non_commands"]:
        return path
    document["non_commands"].append(key)
    return _write(path, document)
