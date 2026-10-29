"""Learning reviews local fake logs and only writes reviewed local config."""

import json
import os
from pathlib import Path

import pytest
import yaml

from voicebar import learning
from voicebar.catalog import Catalog
from voicebar.learning import (
    LearningError,
    mark_not_command,
    recent_entries,
    save_alias,
)


@pytest.fixture
def catalog():
    return Catalog(
        apps=["Notepad", "Calculator"],
        sites={"youtube": "https://www.youtube.com"},
        engines={"google": "https://google.com/search?q={q}",
                 "youtube": "https://www.youtube.com/results?search_query={q}"},
    )


def log(root, rows, name="listen-test.jsonl"):
    path = root / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")
    return path


def success(text, plan=None, **extra):
    plan = plan or [{"action": "open_app", "app": "Notepad"}]
    return {"event": "decision", "text": text, "reason": "run", "tier": "tier2",
            "plan": plan, "dry_run": False, "executed": True,
            "results": [{"ok": True} for _ in plan], **extra}


def test_recent_unresolved_raw_name_removal_and_counts(tmp_path):
    log(tmp_path, [
        {"text": "Computer,   type Hello,  World!", "reason": "unresolved"},
        {"text": "hey COMPUTER,   type Hello,  World!", "reason": "unresolved"},
        {"text": "never mind", "state": "not sure"},
        {"text": "computer", "reason": "unresolved"},
        {"text": "open Calculator", "reason": "ignored"},
    ])
    entries = recent_entries(tmp_path, ["computer"])
    assert [(row.phrase, row.count, row.reason, row.plan) for row in entries] == [
        ("never mind", 1, "not sure", None),
        ("type Hello,  World!", 2, "unresolved", None),
    ]


def test_missing_wake_name_is_preserved_without_inventing_one(tmp_path):
    log(tmp_path, [{"text": "never mind", "reason": "unresolved"}])
    assert recent_entries(tmp_path, ["computer"])[0].phrase == "never mind"


def test_repeated_successful_tier2_plan_offered_after_three_runs(tmp_path):
    log(tmp_path, [success("computer pull up my editor") for _ in range(3)])
    [entry] = recent_entries(tmp_path, ["computer"])
    assert entry.phrase == "pull up my editor"
    assert entry.count == 3
    assert entry.reason == "recurring tier 2"
    assert entry.plan == [{"action": "open_app", "app": "Notepad"}]


@pytest.mark.parametrize("extra", [
    {"dry_run": True},
    {"executed": False},
    {"results": [{"ok": False}]},
    {"results": []},
    {"results": [{"ok": True}, {"ok": True}]},
    {"tier": "tier1"},
])
def test_dry_failed_incomplete_and_tier1_runs_never_offer_alias(tmp_path, extra):
    log(tmp_path, [success("computer pull up my editor", **extra) for _ in range(3)])
    assert recent_entries(tmp_path, ["computer"]) == []


def test_fewer_than_three_or_conflicting_successes_do_not_teach(tmp_path):
    log(tmp_path, [success("computer pull up my editor") for _ in range(2)])
    assert recent_entries(tmp_path, ["computer"]) == []
    log(tmp_path, [success("computer pull up my editor") for _ in range(3)] + [
        success("computer pull up my editor", [{"action": "open_app", "app": "Calculator"}]),
        {"text": "computer pull up my editor", "reason": "unresolved"},
    ])
    [entry] = recent_entries(tmp_path, ["computer"])
    assert entry.count == 5 and entry.plan is None


def test_hallucinated_logged_payload_is_not_a_suggestion(tmp_path):
    bad = [{"action": "type_text", "text": "HALLUCINATED", "submit": False}]
    log(tmp_path, [success("computer write Hello!", bad) for _ in range(3)])
    assert recent_entries(tmp_path, ["computer"]) == []


def test_malformed_rows_and_huge_lines_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "MAX_LINE_BYTES", 256)
    path = log(tmp_path, [{"text": "computer fine", "reason": "unresolved"}])
    with path.open("ab") as handle:
        handle.write(b"not JSON\n[]\n\xff\n" + b"x" * 1000 + b"\n")
        handle.write(json.dumps({"text": "computer corrupt", "reason": [], "tier": []}).encode() + b"\n")
        handle.write(json.dumps({"text": None, "reason": "unresolved"}).encode() + b"\n")
    assert [entry.phrase for entry in recent_entries(tmp_path, ["computer"])] == ["fine"]


def test_reading_large_log_uses_only_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "MAX_FILE_BYTES", 256)
    path = log(tmp_path, [{"text": "computer ancient", "reason": "unresolved"}])
    with path.open("ab") as handle:
        handle.write(b"x" * 2000 + b"\n")
        handle.write(json.dumps({"text": "computer recent", "reason": "unresolved"}).encode() + b"\n")
    assert [entry.phrase for entry in recent_entries(tmp_path, ["computer"])] == ["recent"]


def test_only_newest_files_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "MAX_LOG_FILES", 1)
    older = log(tmp_path, [{"text": "computer old", "reason": "unresolved"}], "old.jsonl")
    newer = log(tmp_path, [{"text": "computer new", "reason": "unresolved"}], "new.jsonl")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    assert [entry.phrase for entry in recent_entries(tmp_path, ["computer"])] == ["new"]


def test_save_explicit_app_choice_and_mark_negative(tmp_path, catalog):
    path = save_alias(tmp_path, "Pull  up my Editor!",
                      [{"action": "open_app", "app": "Notepad"}], catalog)
    assert path == tmp_path / "config" / "learned.yaml"
    mark_not_command(tmp_path, "That is not what I meant.")
    assert yaml.safe_load(path.read_text()) == {
        "aliases": {"pull up my editor": [{"action": "open_app", "app": "Notepad"}]},
        "non_commands": ["that is not what i meant"],
    }
    assert not list(path.parent.glob(".learned-*.tmp"))


def test_identical_choices_are_idempotent_and_conflicts_preserve_file(tmp_path, catalog):
    first = [{"action": "open_app", "app": "Notepad"}]
    path = save_alias(tmp_path, "pull up my editor", first, catalog)
    before = path.read_bytes()
    assert save_alias(tmp_path, "PULL up my editor", first, catalog) == path
    with pytest.raises(LearningError, match="different learned alias"):
        save_alias(tmp_path, "pull up my editor", [{"action": "open_app", "app": "Calculator"}], catalog)
    with pytest.raises(LearningError, match="already has a learned alias"):
        mark_not_command(tmp_path, "pull up my editor")
    assert path.read_bytes() == before
    mark_not_command(tmp_path, "never mind")
    before = path.read_bytes()
    mark_not_command(tmp_path, "Never mind!")
    with pytest.raises(LearningError, match="not a command"):
        save_alias(tmp_path, "never mind", first, catalog)
    assert path.read_bytes() == before


@pytest.mark.parametrize("plan", [
    [{"action": "open_app", "app": "Unknown"}],
    [{"action": "window", "op": "maximize", "target": "Unknown"}],
    [{"action": "open_url", "url": "https://unknown.example"}],
    [{"action": "web_search", "engine": "unknown", "query": "Hello"}],
    [{"action": "tab", "op": "close", "site": "unknown"}],
    [{"action": "type_text", "text": "INVENTED", "submit": False}],
    [{"action": "type_text", "text": "hello", "submit": False}],
    [{"action": "shell", "command": "anything"}],
    [{"action": "open_app", "app": "Notepad", "extra": True}],
    [{"action": "app_action", "app": "Notepad", "name": "unknown"}],
    [], None,
])
def test_invalid_targets_schema_and_invented_payloads_are_never_saved(tmp_path, catalog, plan):
    with pytest.raises(LearningError):
        save_alias(tmp_path, "write Hello!", plan, catalog)
    assert not (tmp_path / "config" / "learned.yaml").exists()


def test_type_alias_preserves_exact_raw_payload(tmp_path, catalog):
    plan = [{"action": "type_text", "text": "Hello,  World!", "submit": False}]
    path = save_alias(tmp_path, "type Hello,  World!", plan, catalog)
    assert yaml.safe_load(path.read_text())["aliases"]["type hello world"] == plan


def test_substring_without_type_or_search_cue_is_not_a_payload_candidate(tmp_path, catalog):
    with pytest.raises(LearningError, match="code-built"):
        save_alias(tmp_path, "Hello is the person's name",
                   [{"action": "type_text", "text": "Hello", "submit": False}], catalog)


def test_atomic_replace_failure_preserves_previous_choices_and_cleans_temp(tmp_path, catalog, monkeypatch):
    path = mark_not_command(tmp_path, "never mind")
    before = path.read_bytes()
    original_replace = Path.replace

    def fail_replace(source, destination):
        if Path(destination) == path:
            raise OSError("simulated sharing violation")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(LearningError, match="sharing violation"):
        save_alias(tmp_path, "bring up my editor", [{"action": "open_app", "app": "Notepad"}], catalog)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".learned-*.tmp"))


@pytest.mark.parametrize("text", [
    "aliases: []\nnon_commands: []\n",
    "aliases: {}\nnon_commands: []\nextra: true\n",
    "aliases: {}\naliases: {}\nnon_commands: []\n",
    "aliases: {}\nnon_commands: [Never mind, never mind]\n",
    "aliases: {'never mind': [{action: open_app, app: Notepad}]}\nnon_commands: [never mind]\n",
])
def test_bad_existing_configuration_is_preserved(tmp_path, text):
    path = tmp_path / "config" / "learned.yaml"
    path.parent.mkdir()
    path.write_text(text)
    with pytest.raises(LearningError):
        mark_not_command(tmp_path, "not a command")
    assert path.read_text() == text


def test_missing_logs_return_empty_list(tmp_path):
    assert recent_entries(tmp_path, ["computer"]) == []
