"""Unsafe request forms cannot reach even an always-action selector."""

import json

import pytest

from voicebar.catalog import Catalog
from voicebar.resolver.base import Resolver


@pytest.mark.parametrize("text", [
    "what is the weather like",
    "how many monitors do I have",
    "should I close the editor before leaving",
    "I prefer quieter music",
    "I don't want another tab",
    "I was going to open Notepad later",
    "maybe I'll search for robotics tomorrow",
    "the next track is my favorite",
    "my friend said to maximize the window",
    "if I wanted to, I could move this window",
    "never mind",
    "that is not what I meant",
    "open Notepad and don't close Calculator",
    "fire up Notepad and delete my files",
    "bring Calculator forward then install a package",
    "show me how to type Hello",
])
def test_nonrequests_never_reach_selector(text):
    class AlwaysActs:
        def select(self, request, timeout_s):
            pytest.fail("Rejected speech reached the selector")

    resolver = Resolver(AlwaysActs())
    assert resolver.resolve("computer, " + text, Catalog(apps=["Notepad", "Calculator"])) is None
    assert resolver.last_outcome == "non_request"


def test_negation_and_questions_inside_search_payload_are_literal_data():
    text = "hunt for why Chrome should not close"
    expected = {"action": "web_search", "engine": "google", "query": "why Chrome should not close"}

    class Selector:
        def select(self, request, timeout_s):
            chosen = next(candidate.id for candidate in request.candidates if candidate.action == expected)
            return json.dumps({"actions": [chosen]})

    catalog = Catalog(apps=["Chrome"], engines={"google": "https://example.test/?q={q}"})
    assert Resolver(Selector()).resolve("computer, " + text, catalog) == [expected]
