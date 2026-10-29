"""Definitions cannot turn fixed typing into terminal command execution."""

import pytest

from voicebar.app_actions import AppActionError, load_app_actions, validate_definition

# Names verified in this machine's .cache/app-identities.json. The test never
# starts any of these applications or executes the benign example command.
SHELL_HOSTS = [
    "Command Prompt", "Windows PowerShell", "Windows PowerShell ISE",
    "Windows PowerShell (x86)", "Windows PowerShell ISE (x86)", "Terminal",
    "Anaconda Prompt", "Anaconda PowerShell Prompt", "Developer PowerShell for VS 2022",
    "Developer Command Prompt for VS 2022", "Node.js command prompt",
    "x86 Native Tools Command Prompt for VS 2022",
    "x64 Native Tools Command Prompt for VS 2022",
    "x86_x64 Cross Tools Command Prompt for VS 2022",
    "x64_x86 Cross Tools Command Prompt for VS 2022",
]


def definition(app):
    return {"app": app, "actions": {"fixed_command": {
        "say": ["fixed command"],
        "do": [{"type": "Write-Output voicebar"}, {"keys": "enter"}],
    }}}


@pytest.mark.parametrize("app", SHELL_HOSTS)
def test_known_shell_host_rejects_fixed_typing_and_enter_at_load(app):
    with pytest.raises(AppActionError, match="(?i)(shell|command.host|terminal)"):
        validate_definition(definition(app), [app])


@pytest.mark.parametrize("app", ["Notepad", "Calculator", "Claude", "Word"])
def test_fixed_text_remains_permitted_in_ordinary_app_definitions(app):
    supplied = definition(app)
    supplied["actions"]["fixed_command"]["do"] = [{"type": "Voicebar fixed text"}]
    name, registry = validate_definition(supplied, [app])
    assert name == app
    assert registry["fixed_command"]["do"] == [{"type": "Voicebar fixed text"}]


def test_shell_host_file_rejects_registry_load_before_actions_are_available(tmp_path):
    path = tmp_path / "powershell.yaml"
    path.write_text(
        "app: Windows PowerShell\nactions:\n  fixed_command:\n"
        "    say: [fixed command]\n    do:\n"
        "      - type: Write-Output voicebar\n      - keys: enter\n", encoding="utf-8")
    with pytest.raises(AppActionError, match="(?i)(shell|command.host|terminal)"):
        load_app_actions(tmp_path, ["Windows PowerShell"])
