"""App-action data validation without touching a real window or input device."""
import json
from copy import deepcopy

import pytest
import yaml

from voicebar.app_actions import AppActionError, load_app_actions, validate_definition
from voicebar.catalog import Catalog, CatalogError, load_catalog

INSTALLED = ["Claude", "Google Chrome", "Notepad"]


@pytest.fixture
def definition():
    return {
        "app": "Claude",
        "actions": {
            "start_voice": {
                "say": ["Start voice mode", "start voice input"],
                "do": [
                    {"keys": "ctrl+shift+v"},
                    {"wait": {"name": "Voice input", "role": "Button", "timeout": 5}},
                    {"click": {"automation_id": "voice-input", "name": "Voice input"}},
                    {"type": "Fixed text, with Case and punctuation!"},
                ],
                "then": "sleep",
            },
        },
    }


@pytest.fixture
def directory(tmp_path):
    target = tmp_path / "apps"
    target.mkdir()
    return target


def write_definition(directory, value, name="claude.yaml"):
    path = directory / name
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_valid_definition_copies_plain_steps_and_normalizes_only_spoken_phrases(definition):
    original = deepcopy(definition)
    app, actions = validate_definition(definition, INSTALLED)
    assert app == "Claude"
    assert actions["start_voice"] == {
        "say": ["start voice mode", "start voice input"],
        "do": original["actions"]["start_voice"]["do"],
        "then": "sleep",
    }
    actions["start_voice"]["do"][1]["wait"]["name"] = "Changed"
    assert definition == original


def test_loads_every_app_and_allows_same_phrase_for_different_apps(directory, definition):
    write_definition(directory, definition)
    other = {"app": "Notepad", "actions": {
        "start_voice": {"say": ["start voice mode"], "do": [{"keys": "ctrl+n"}]},
    }}
    write_definition(directory, other, "notepad.yaml")
    registry = load_app_actions(directory, INSTALLED)
    assert set(registry) == {"Claude", "Notepad"}
    assert registry["Notepad"]["start_voice"]["do"] == [{"keys": "ctrl+n"}]


def test_missing_directory_is_an_empty_optional_registry(tmp_path):
    assert load_app_actions(tmp_path / "absent", INSTALLED) == {}
    assert Catalog().app_actions == {}


def test_definition_directory_must_be_a_directory(tmp_path):
    path = tmp_path / "apps"
    path.write_text("", encoding="utf-8")
    with pytest.raises(AppActionError, match="must be a directory"):
        load_app_actions(path, INSTALLED)


@pytest.mark.parametrize("app,installed,match", [
    ("missing", INSTALLED, "not installed"),
    ("claude", INSTALLED, "exact name"),
    ("cloud", INSTALLED, "exact name"),
    ("Claude", INSTALLED + ["CLAUDE"], "ambiguous"),
    ("Claude", INSTALLED + ["Claude"], "ambiguous"),
    ("active", INSTALLED + ["active"], "reserved"),
])
def test_app_identity_is_canonical_unique_and_not_reserved(definition, app, installed, match):
    definition["app"] = app
    with pytest.raises(AppActionError, match=match):
        validate_definition(definition, installed)


@pytest.mark.parametrize("name", ["click:Save", "switch_tab:Example"])
def test_dynamic_action_names_are_reserved(definition, name):
    definition["actions"][name] = definition["actions"].pop("start_voice")
    with pytest.raises(AppActionError, match="reserved"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("name", ["", "Start Voice", "new-chat", "2start", 123])
def test_action_ids_have_a_single_unambiguous_spelling(definition, name):
    definition["actions"][name] = definition["actions"].pop("start_voice")
    with pytest.raises(AppActionError, match="action names|mapping keys must be text"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("value", [None, [], "start voice", {"start voice": "start_voice"}])
def test_say_requires_a_list_of_spoken_phrases(definition, value):
    definition["actions"]["start_voice"]["say"] = value
    with pytest.raises(AppActionError, match="nonempty list"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("phrases", [["start voice", " START voice! "], ["startvoice", "start voice"]])
def test_duplicate_phrases_are_rejected_after_parser_normalization(definition, phrases):
    definition["actions"]["start_voice"]["say"] = phrases
    with pytest.raises(AppActionError, match="duplicate phrase"):
        validate_definition(definition, INSTALLED)


def test_different_actions_cannot_claim_the_same_spoken_phrase(definition):
    definition["actions"]["other"] = {"say": ["START VOICE MODE"], "do": [{"keys": "ctrl+n"}]}
    with pytest.raises(AppActionError, match="duplicate phrase"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("phrase", ["!!!", "", 3])
def test_say_has_to_contain_actual_words(definition, phrase):
    definition["actions"]["start_voice"]["say"] = [phrase]
    with pytest.raises(AppActionError, match="nonempty text|spoken word"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("steps", [None, [], {}, "ctrl+n", [{"shell": "cmd.exe"}],
                                    [{"run": "powershell.exe"}], [{"keys": "ctrl+n", "type": "text"}], [{}]])
def test_only_ordered_nonempty_closed_steps_are_allowed(definition, steps):
    definition["actions"]["start_voice"]["do"] = steps
    with pytest.raises(AppActionError, match="ordered list|exactly one"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("text", ["{transcript}", "{{ heard }}", "$transcript", "${utterance}",
                                  "%transcript%", "<%= transcript %>", "<<text>>"])
def test_type_cannot_request_transcript_interpolation(definition, text):
    definition["actions"]["start_voice"]["do"] = [{"type": text}]
    with pytest.raises(AppActionError, match="fixed text"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("text", [{"transcript": True}, None, "", 4, "bad\x00text"])
def test_type_is_only_a_nonempty_literal_string(definition, text):
    definition["actions"]["start_voice"]["do"] = [{"type": text}]
    with pytest.raises(AppActionError, match="nonempty text|control character"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("keys", ["ctrl++n", "Ctrl+n", "ctrl+ctrl+n", "f25", "f0", "ctrl+é", "cmd+n",
                                  "win+r", "r+win", "win+x", "win", "win+e", "ctrl+esc"])
def test_unsupported_or_system_command_hotkeys_are_rejected(definition, keys):
    definition["actions"]["start_voice"]["do"] = [{"keys": keys}]
    with pytest.raises(AppActionError):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("selector", [{}, {"name": ""}, {"name": 123}, {"path": "0/1"},
                                      {"name": "Save", "timeout": 1}])
def test_click_uses_only_nonempty_exact_selector_fields(definition, selector):
    definition["actions"]["start_voice"]["do"] = [{"click": selector}]
    with pytest.raises(AppActionError):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("timeout", [None, True, 0, -1, 30.01, float("nan"), float("inf"), "3"])
def test_wait_timeout_is_required_finite_and_bounded(definition, timeout):
    definition["actions"]["start_voice"]["do"] = [{"wait": {"name": "Ready", "timeout": timeout}}]
    with pytest.raises(AppActionError, match="timeout"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("then", [None, "wake", "shell", ["sleep"], {"mode": "sleep"}])
def test_then_cannot_add_another_action_family(definition, then):
    definition["actions"]["start_voice"]["then"] = then
    with pytest.raises(AppActionError, match="only sleep"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("where", ["top", "action", "selector"])
def test_unknown_fields_are_rejected_at_every_mapping_level(definition, where):
    target = definition if where == "top" else definition["actions"]["start_voice"]
    if where == "selector":
        target = target["do"][2]["click"]
    target["command"] = "cmd.exe"
    with pytest.raises(AppActionError, match="only"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("text", [
    "app: Claude\napp: Notepad\nactions: {}\n",
    "app: Claude\nactions:\n  new_chat:\n    say: [new chat]\n    do:\n      - keys: ctrl+n\n        keys: ctrl+o\n",
    "app: Claude\nactions:\n  new_chat:\n    say: [new chat]\n    do:\n      - click: {name: Save, name: Open}\n",
])
def test_duplicate_yaml_keys_are_never_silently_overwritten(directory, text):
    (directory / "claude.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(AppActionError, match="duplicate key"):
        load_app_actions(directory, INSTALLED)


def test_yaml_cannot_construct_python_objects(directory):
    (directory / "claude.yaml").write_text("!!python/object:builtins.object {}", encoding="utf-8")
    with pytest.raises(AppActionError, match="Cannot read claude.yaml"):
        load_app_actions(directory, INSTALLED)


def test_two_files_cannot_replace_each_others_app_actions(directory, definition):
    write_definition(directory, definition)
    write_definition(directory, definition, "other.yaml")
    with pytest.raises(AppActionError, match="duplicate app definition"):
        load_app_actions(directory, INSTALLED)


def test_validation_does_not_create_a_native_input_backend(definition, monkeypatch):
    from voicebar.executor import input as input_module

    def forbidden(*args, **kwargs):
        pytest.fail("Definition validation must not create a native input backend")

    monkeypatch.setattr(input_module.InputBackend, "__init__", forbidden)
    assert validate_definition(definition, INSTALLED)[0] == "Claude"


@pytest.mark.parametrize("command", ["new_design", "create_sketch", "fit_view"])
def test_fusion_accepts_only_named_code_owned_mcp_commands(definition, command, monkeypatch):
    from voicebar import fusion_mcp

    def forbidden(*args, **kwargs):
        pytest.fail("Loading definitions must not call the local Fusion endpoint")

    monkeypatch.setattr(fusion_mcp.FusionMCPClient, "call_command", forbidden)
    definition["app"] = "Autodesk Fusion"
    definition["actions"]["start_voice"]["do"] = [{"mcp": {"command": command}}]
    app, actions = validate_definition(definition, ["Autodesk Fusion"])
    assert app == "Autodesk Fusion"
    assert actions["start_voice"]["do"] == [{"mcp": {"command": command}}]


@pytest.mark.parametrize("payload", [
    "new_design", {}, {"command": None}, {"command": []},
    {"command": "arbitrary_unlisted_command"}, {"command": "{transcript}"},
    {"tool": "fusion_mcp_execute", "arguments": {"script": "arbitrary code"}},
    {"command": "new_design", "arguments": {}},
    {"command": "new_design", "script": "arbitrary code"},
    {"command": "new_design", "endpoint": "https://example.invalid/mcp"},
    {"command": "new_design", "token": "secret"},
])
def test_fusion_mcp_cannot_take_arbitrary_tools_scripts_arguments_or_remote_endpoints(definition, payload):
    definition["app"] = "Autodesk Fusion"
    definition["actions"]["start_voice"]["do"] = [{"mcp": payload}]
    with pytest.raises(AppActionError):
        validate_definition(definition, ["Autodesk Fusion"])


def test_mcp_exception_is_scoped_to_exact_installed_fusion_app(definition):
    definition["actions"]["start_voice"]["do"] = [{"mcp": {"command": "new_design"}}]
    with pytest.raises(AppActionError, match="only permitted for Autodesk Fusion"):
        validate_definition(definition, INSTALLED)


@pytest.mark.parametrize("app", ["Python 3.11 (64-bit)", "Python 3.12 (64-bit)", "Python 3.14 (64-bit)",
                                 "IDLE (Python 3.12 64-bit)", "Node.js"])
def test_installed_script_interpreters_cannot_be_app_action_targets(definition, app):
    definition["app"] = app
    with pytest.raises(AppActionError, match="shell/command-host"):
        validate_definition(definition, [app])


@pytest.mark.parametrize("app", ["Python 3.12 Manuals (64-bit)", "Python 3.12 Module Docs (64-bit)",
                                 "Node.js documentation", "Node.js website"])
def test_interpreter_help_apps_are_not_mistaken_for_command_hosts(definition, app):
    definition["app"] = app
    assert validate_definition(definition, [app])[0] == app


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache/startapps.json").write_text(json.dumps([
        {"Name": name, "AppID": name} for name in INSTALLED
    ]), encoding="utf-8")
    config = tmp_path / "config"
    (config / "apps").mkdir(parents=True)
    for name, value in {"aliases.yaml": {}, "sites.yaml": {}, "pinned.yaml": [],
                        "settings.yaml": {"engines": {"google": "https://example.com/?q={q}"}}}.items():
        (config / name).write_text(yaml.safe_dump(value), encoding="utf-8")
    return tmp_path


def test_catalog_exposes_verified_definition_data(project, definition):
    write_definition(project / "config/apps", definition)
    catalog = load_catalog(project).catalog
    assert catalog.app_actions["Claude"]["start_voice"]["then"] == "sleep"


def test_catalog_rejects_all_definitions_if_one_is_invalid(project, definition):
    write_definition(project / "config/apps", definition)
    write_definition(project / "config/apps", {"app": "missing", "actions": {}}, "missing.yaml")
    with pytest.raises(CatalogError, match="not installed"):
        load_catalog(project)


def test_learned_action_cannot_bypass_verified_app_definition_scope(project, definition):
    write_definition(project / "config/apps", definition)
    (project / "config/learned.yaml").write_text(yaml.safe_dump({"aliases": {
        "do it": [{"action": "app_action", "app": "Claude", "name": "start_voice"}],
    }}), encoding="utf-8")
    with pytest.raises(CatalogError, match="Learned app_action is forbidden"):
        load_catalog(project)
