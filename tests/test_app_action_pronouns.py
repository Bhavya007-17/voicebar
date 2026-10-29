"""An app selected by an in-app action becomes the subsequent 'it' target."""

import pytest

from voicebar.app_gate import Stage7Gate as Gate
from voicebar.catalog import Catalog
from voicebar.parser import parse


@pytest.fixture
def catalog():
    return Catalog(apps=["Claude", "Notepad", "Google Chrome"],
                   aliases={"cloud": "Claude", "chrome": "Google Chrome"},
                   app_actions={"Claude": {"new_chat": {
                       "say": ["new chat"], "do": [{"keys": "ctrl+n"}],
                   }}})


@pytest.mark.parametrize("tail, expected", [
    ("minimize it", {"action": "window", "op": "minimize", "target": "Claude"}),
    ("close it", {"action": "window", "op": "close", "target": "Claude"}),
    ("make it full screen", {"action": "window", "op": "maximize", "target": "Claude"}),
])
def test_explicit_in_app_action_overrides_previous_utterance_pronoun(catalog, tail, expected):
    assert parse("new chat in cloud and " + tail, catalog, last_app="Notepad") == [
        {"action": "app_action", "app": "Claude", "name": "new_chat"}, expected,
    ]


def test_chrome_tab_title_action_supplies_following_pronoun(catalog):
    assert parse("switch to the tab named Notes in chrome then minimize it",
                 catalog, last_app="Notepad") == [
        {"action": "app_action", "app": "Google Chrome", "name": "switch_tab:Notes"},
        {"action": "window", "op": "minimize", "target": "Google Chrome"},
    ]


def test_generic_click_keeps_previous_named_app_for_pronouns(catalog):
    assert parse("click One", catalog, last_app="Notepad") == [
        {"action": "app_action", "app": "active", "name": "click:One"},
    ]
    gate = Gate(catalog, ["computer"])
    gate.last_app = "Notepad"
    assert gate.hear("computer click One", 0).reason == "run"
    assert gate.last_app == "Notepad"


def test_explicit_app_action_persists_pronoun_into_next_utterance(catalog):
    gate = Gate(catalog, ["computer"])
    gate.last_app = "Notepad"
    assert gate.hear("computer new chat in Claude", 0).plan == [
        {"action": "app_action", "app": "Claude", "name": "new_chat"},
    ]
    assert gate.last_app == "Claude"
    assert gate.hear("minimize it", 1).plan == [
        {"action": "window", "op": "minimize", "target": "Claude"},
    ]
