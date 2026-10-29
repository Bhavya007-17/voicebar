"""Teaching is explicit, local and reloadable by tier 1."""
import json

import pytest

from voicebar import cli
from voicebar.catalog import load_catalog
from voicebar.parser import parse


@pytest.fixture
def project(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / ".cache").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / ".cache/startapps.json").write_text(json.dumps([
        {"Name": "Notepad", "AppID": "notepad"}]))
    for name, text in {
        "settings.yaml": "names: [computer]\nengines: {google: 'https://google.com/?q={q}'}\n",
        "aliases.yaml": "{}\n", "sites.yaml": "{}\n", "pinned.yaml": "[]\n",
    }.items():
        (tmp_path / "config" / name).write_text(text)
    (tmp_path / "logs/listen-test.jsonl").write_text(json.dumps({
        "event": "decision", "text": "computer, get notepad going", "reason": "unresolved"}) + "\n")
    return tmp_path


def test_list_does_not_write_then_explicit_alias_reloads(project, capsys):
    assert cli.main(["teach", "--list"], project_root=project) == 0
    assert "get notepad going" in capsys.readouterr().out
    assert not (project / "config/learned.yaml").exists()
    assert cli.main(["teach", "--select", "1", "--alias", "Notepad"], project_root=project) == 0
    catalog = load_catalog(project).catalog
    assert parse("get notepad going", catalog) == [{"action": "open_app", "app": "Notepad"}]


def test_explicit_noncommand_reloads(project):
    assert cli.main(["teach", "--select", "1", "--not-command"], project_root=project) == 0
    assert "get notepad going" in load_catalog(project).catalog.noncommands


def test_unknown_app_never_saves(project):
    assert cli.main(["teach", "--select", "1", "--alias", "Invented"], project_root=project) == 2
    assert not (project / "config/learned.yaml").exists()


def test_alias_can_map_unresolved_speech_to_any_supported_command(project):
    assert cli.main(["teach", "--select", "1", "--alias", "close Notepad"], project_root=project) == 0
    assert parse("get notepad going", load_catalog(project).catalog) == [
        {"action": "close_app", "app": "Notepad"},
    ]


def test_unsupported_or_partial_alias_command_never_saves(project):
    assert cli.main(["teach", "--select", "1", "--alias", "open Notepad and delete files"], project_root=project) == 2
    assert not (project / "config/learned.yaml").exists()


def test_declining_interactive_teaching_is_read_only(project, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert cli.main(["teach"], project_root=project) == 0
    assert not (project / "config/learned.yaml").exists()
