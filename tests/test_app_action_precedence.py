"""Explicit app scopes and exact phrases outrank overlapping payload grammar."""

import pytest

from voicebar.catalog import Catalog
from voicebar.parser import parse


@pytest.fixture
def catalog():
    return Catalog(apps=["Google Chrome", "Claude"], aliases={"chrome": "Google Chrome"},
                   engines={"google": "https://www.google.com/search?q={q}"},
                   app_actions={"Google Chrome": {"search_tabs": {
                       "say": ["search tabs"], "do": [{"keys": "ctrl+shift+a"}],
                   }}})


@pytest.mark.parametrize("foreground", ["Google Chrome", "Claude", None])
def test_explicit_chrome_tab_title_is_not_also_parsed_as_foreground_title(catalog, foreground):
    assert parse("switch to the tab named Voicebar Alpha in chrome", catalog,
                 active_app=foreground) == [
        {"action": "app_action", "app": "Google Chrome", "name": "switch_tab:Voicebar Alpha"},
    ]


@pytest.mark.parametrize("text, foreground", [
    ("search tabs in chrome", None),
    ("search tabs in chrome", "Google Chrome"),
    ("search tabs", "Google Chrome"),
])
def test_exact_configured_search_tabs_phrase_precedes_web_search(catalog, text, foreground):
    assert parse(text, catalog, active_app=foreground) == [
        {"action": "app_action", "app": "Google Chrome", "name": "search_tabs"},
    ]


@pytest.mark.parametrize("text, query", [
    ("search tabs for Voicebar notes", "tabs for Voicebar notes"),
    ("search tabs in chrome for Voicebar notes", "tabs in chrome for Voicebar notes"),
    ("search cats and open Claude", "cats and open Claude"),
])
def test_other_search_utterances_still_swallow_all_payload(catalog, text, query):
    assert parse(text, catalog, active_app="Google Chrome") == [
        {"action": "web_search", "engine": "google", "query": query},
    ]


def test_type_payload_preserves_phrase_matching_an_app_action(catalog):
    assert parse("type Search tabs in chrome and press enter", catalog,
                 active_app="Google Chrome") == [
        {"action": "type_text", "text": "Search tabs in chrome", "submit": True},
    ]
