"""Actual microphone transcription must not launch Voice access by splitting."""

from pathlib import Path

import pytest

from voicebar.app_actions import load_app_actions
from voicebar.app_gate import Stage7Gate
from voicebar.catalog import Catalog
from voicebar.listening import Chimes, GateSession
from voicebar.parser import parse


@pytest.fixture
def catalog():
    return Catalog(
        apps=["Claude", "Voice access", "Notepad"],
        app_actions={"Claude": {"voice": {
            "say": ["start voice mode", "start voice input"],
            "do": [{"click": {"name": "Dictate"}}],
        }}},
    )


@pytest.mark.parametrize("foreground", ["Claude", "Notepad", None])
@pytest.mark.parametrize("text", [
    "start voice", "start voice dictate mode", "start voice dictate mode in Claude",
    "open Claude and start voice dictate mode", "open Claude start voice dictate mode",
    "open Claude and start voice delete everything",
])
def test_unfinished_app_phrase_cannot_become_open_voice_access_and_type(catalog, foreground, text):
    assert parse(text, catalog, active_app=foreground) is None


@pytest.mark.parametrize("text", [
    "start voice mode", "open Claude and start voice mode",
    "open Claude start voice mode",
])
def test_complete_phrase_still_works(catalog, text):
    plan = parse(text, catalog, active_app="Claude")
    assert plan and plan[-1] == {"action": "app_action", "app": "Claude", "name": "voice"}


def test_explicit_complete_app_name_and_typing_are_preserved(catalog):
    assert parse("start Voice access and dictate mode", catalog, active_app="Claude") == [
        {"action": "open_app", "app": "Voice access"},
        {"action": "type_text", "text": "mode", "submit": False},
    ]
    assert parse("type start voice dictate mode", catalog, active_app="Claude") == [
        {"action": "type_text", "text": "start voice dictate mode", "submit": False},
    ]


def test_unfinished_configured_phrase_cannot_override_a_literal_typing_payload(catalog):
    catalog.app_actions["Claude"]["fixed_text"] = {
        "say": ["write fixed text"], "do": [{"type": "fixed"}],
    }
    assert parse("write fixed", catalog, active_app="Claude") == [
        {"action": "type_text", "text": "fixed", "submit": False},
    ]


def test_observed_recognizer_variant_is_an_exact_local_configured_phrase(catalog):
    catalog.app_actions = load_app_actions(
        Path(__file__).parents[1] / "config/apps",
        ["Claude", "Google Chrome", "Word", "Autodesk Fusion"],
    )
    assert parse("Open Claude and start voice dictate mode.", catalog) == [
        {"action": "open_app", "app": "Claude"},
        {"action": "app_action", "app": "Claude", "name": "start_voice_input"},
    ]


def test_unknown_app_action_cannot_be_reinterpreted_by_generic_selector(catalog):
    calls = []

    def resolver(*args):
        calls.append("resolver")
        return [{"action": "open_app", "app": "Voice access"}]

    session = GateSession(
        Stage7Gate(catalog, ["computer"]), resolver=resolver,
        execute=lambda plan: calls.append(plan), clock=lambda: 10,
        foreground_app=lambda: "Claude", chimes=Chimes(clock=lambda: 10, play=lambda: None),
    )
    row = session.hear("computer open Claude and start voice dictate mode",
                       start=8, end=9, speech_end=9, generation=session.generation)
    assert row["reason"] == "unresolved" and row["plan"] is None
    assert not calls
