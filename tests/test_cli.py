"""CLI integration against an isolated installed-app/config snapshot."""

import json
from types import SimpleNamespace

import pytest

from voicebar import cli
from voicebar.catalog import Catalog, CatalogError
from voicebar.diagnostics import explain_failure


@pytest.fixture
def installed(monkeypatch):
    catalog = Catalog(
        apps=["Google Chrome", "Autodesk Fusion", "Microsoft Edge", "Microsoft Teams", "Claude"],
        aliases={"browser": "Google Chrome", "fusion": "Autodesk Fusion", "cloud": "Claude"},
        sites={"github": "https://github.com"},
        engines={"google": "https://www.google.com/search?q={q}"},
    )
    loaded = SimpleNamespace(
        catalog=catalog,
        installed_apps=[SimpleNamespace(name=name, app_id=f"id:{name}") for name in catalog.apps],
    )
    calls = []

    def load(root, refresh=False):
        calls.append((root, refresh))
        return loaded

    monkeypatch.setattr(cli, "load_catalog", load)
    return loaded, calls


def test_plan_prints_exact_three_actions_without_execution(installed, capsys, tmp_path):
    assert cli.main(
        ["plan", "open fusion, make it full screen, and move it to the other screen"],
        project_root=tmp_path,
    ) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == [
        {"action": "open_app", "app": "Autodesk Fusion"},
        {"action": "window", "op": "maximize", "target": "Autodesk Fusion"},
        {"action": "window", "op": "move_monitor", "target": "Autodesk Fusion", "monitor": "next"},
    ]
    assert output.err == ""


@pytest.mark.parametrize("text", ["open chromeee", "open microsoft"])
def test_failed_targets_print_null_and_scored_candidates(text, installed, capsys, tmp_path):
    assert cli.main(["plan", text], project_root=tmp_path) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) is None
    explanation = json.loads(output.err)
    assert explanation["reason"] == "unknown_or_ambiguous_target"
    assert explanation["targets"][0]["candidates"]
    assert all(isinstance(item["score"], float) for item in explanation["targets"][0]["candidates"])


@pytest.mark.parametrize("utterance", [
    "open chrome and open zzzunknown", "open github and zzzunknown",
    "open zzzunknown and open chrome",
])
def test_unknown_target_in_chain_is_reported(utterance, installed):
    result = explain_failure(utterance, installed[0].catalog)
    assert result["targets"][0]["query"] == "zzzunknown"


@pytest.mark.parametrize("utterance", [
    "maximize zzzunknown", "make zzzunknown full screen", "minimize zzzunknown",
    "restore zzzunknown", "snap zzzunknown left", "move zzzunknown to the other screen",
])
def test_unknown_window_target_has_candidates(utterance, installed):
    result = explain_failure(utterance, installed[0].catalog)
    assert result["reason"] == "unknown_or_ambiguous_target"
    assert result["targets"][0]["query"] == "zzzunknown"


def test_duplicate_target_explanation_preserves_both_app_ids():
    catalog = Catalog(apps=["Antigravity", "Antigravity", "Antigravity IDE"])
    result = explain_failure("open Antigravity", catalog, {"Antigravity": ["id:one", "id:two"]})
    best = result["targets"][0]["candidates"][0]
    assert best["name"] == "Antigravity"
    assert best["score"] == 100.0
    assert best["app_ids"] == ["id:one", "id:two"]


def test_unknown_grammar_is_not_misrepresented_as_an_app_target(installed):
    assert explain_failure("tell me a joke", installed[0].catalog) == {
        "reason": "unrecognized_command", "targets": []
    }


def test_apps_refresh_and_search_alias(installed, capsys, tmp_path):
    assert cli.main(["apps", "--refresh", "--search", "cloud"], project_root=tmp_path) == 0
    output = capsys.readouterr()
    assert output.out == "Name\tAppID\nClaude\tid:Claude\n"
    assert "1 matching entries; 5 indexed" in output.err
    assert installed[1] == [(tmp_path, True)]


def test_config_error_is_plain_without_traceback(installed, monkeypatch, capsys, tmp_path):
    def fail(*args, **kwargs):
        raise CatalogError("aliases.yaml: missing installed app 'Missing'")

    monkeypatch.setattr(cli, "load_catalog", fail)
    assert cli.main(["plan", "open fusion"], project_root=tmp_path) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "voicebar: aliases.yaml: missing installed app 'Missing'\n"


def test_version_does_not_load_or_refresh_catalog(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("version must not enumerate apps")

    monkeypatch.setattr(cli, "load_catalog", unexpected)
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == "voicebar 0.1.0\n"


def test_run_dry_run_does_not_construct_executor(installed, monkeypatch, capsys, tmp_path):
    def unexpected(*args, **kwargs):
        raise AssertionError("dry-run must not construct executor or read runtime state")

    monkeypatch.setattr("voicebar.executor.common.Context", unexpected)
    monkeypatch.setattr("voicebar.executor.common.read_state", unexpected)
    assert cli.main(["run", "--dry-run", "open fusion"], project_root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == [{"action": "open_app", "app": "Autodesk Fusion"}]
