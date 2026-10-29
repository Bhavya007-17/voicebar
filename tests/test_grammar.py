"""Grammar publication and ambiguous installed-name safety checks."""
from pathlib import Path

import pytest

from voicebar import parser
from voicebar.catalog import Catalog
from voicebar.command_docs import CLAUSE_ACTIONS, render_commands

ROOT = Path(__file__).resolve().parents[1]


def test_generated_command_reference_is_current():
    assert (ROOT / "docs" / "COMMANDS.md").read_text(encoding="utf-8") == render_commands(
        ROOT / "tests" / "test_parser_contract.py"
    )


def test_every_live_clause_pattern_has_an_action_label():
    assert set(CLAUSE_ACTIONS) == set(parser.CLAUSE_PATTERNS)


def test_published_grammar_changes_when_live_pattern_changes(monkeypatch):
    before = render_commands(ROOT / "tests" / "test_parser_contract.py")
    monkeypatch.setattr(parser, "SCROLL", parser.re.compile(r"a distinct grammar pattern"))
    after = render_commands(ROOT / "tests" / "test_parser_contract.py")
    assert before != after
    assert "a distinct grammar pattern" in after


@pytest.mark.parametrize("verb", ["open", "switch to", "close", "maximize", "minimize", "restore"])
@pytest.mark.parametrize("names", [
    ["Antigravity", "Antigravity", "Antigravity IDE"],
    ["Anti gravity", "ANTIGRAVITY", "Antigravity IDE"],
])
def test_duplicate_normalized_app_names_are_ambiguous(verb, names):
    assert parser.parse(f"{verb} Antigravity", Catalog(apps=names)) is None


def test_alias_to_duplicate_app_name_is_ambiguous():
    catalog = Catalog(apps=["Antigravity", "Antigravity"], aliases={"anti": "Antigravity"})
    assert parser.parse("open anti", catalog) is None


def test_noise_removal_does_not_bypass_duplicate_name_check():
    catalog = Catalog(apps=["Antigravity", "Antigravity", "Antigravity IDE"])
    assert parser.parse("open a new Antigravity app", catalog) is None


@pytest.mark.parametrize("name", [
    "Windows PowerShell (x86)",
    "Windows PowerShell ISE (x86)",
    "IDLE (Python 3.11 64-bit)",
    "IDLE (Python 3.12 64-bit)",
    "IDLE (Python 3.14 64-bit)",
    "Python 3.11 Manuals (64-bit)",
    "Python 3.12 Manuals (64-bit)",
    "Python 3.14 Manuals (64-bit)",
    "Python 3.11 Module Docs (64-bit)",
    "Python 3.12 Module Docs (64-bit)",
    "Python 3.14 Module Docs (64-bit)",
])
def test_parenthesized_installed_app_name_is_one_exact_target(name):
    catalog = Catalog(apps=[
        "Windows PowerShell", "Windows PowerShell ISE", "x86 Native Tools Command Prompt",
        "IDLE (Python 3.11 64-bit)", "IDLE (Python 3.12 64-bit)", "IDLE (Python 3.14 64-bit)",
        "Python 3.11 Manuals (64-bit)", "Python 3.12 Manuals (64-bit)",
        "Python 3.14 Manuals (64-bit)", "Python 3.11 Module Docs (64-bit)",
        "Python 3.12 Module Docs (64-bit)", "Python 3.14 Module Docs (64-bit)",
    ])
    if name not in catalog.apps:
        catalog.apps.append(name)
    assert parser.parse(f"open {name}", catalog) == [{"action": "open_app", "app": name}]


@pytest.mark.parametrize("command", ["open", "switch to", "close", "maximize", "minimize", "restore"])
def test_punctuation_normalized_app_name_collision_is_ambiguous(command):
    catalog = Catalog(apps=["Windows PowerShell (x86)", "Windows PowerShell x86"])
    assert parser.parse(f"{command} Windows PowerShell (x86)", catalog) is None


@pytest.mark.parametrize("command", ["open", "switch to", "close"])
def test_exact_ambiguity_cannot_turn_into_two_apps_by_splitting(command):
    catalog = Catalog(apps=[
        "Windows PowerShell", "Windows PowerShell (x86)",
        "Windows PowerShell [x86]", "x86 Native Tools",
    ])
    assert parser.parse(f"{command} Windows PowerShell (x86)", catalog) is None
    assert parser.parse(f"{command} the new Windows PowerShell (x86) app", catalog) is None


def test_public_resolution_keeps_returning_none_for_exact_ambiguity():
    catalog = Catalog(apps=["Windows PowerShell (x86)", "Windows PowerShell [x86]"])
    assert parser.resolve(["windows", "powershell", "x86"], catalog) is None
    assert parser.resolve(["the", "new", "windows", "powershell", "x86", "app"], catalog) is None
