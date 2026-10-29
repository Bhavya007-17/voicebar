"""Conversational framing cannot become permission or alter literal payloads."""
import pytest
from test_conversation import harness


@pytest.mark.parametrize("prefix", ["um", "Uh,", "Ah,", "erm", "er", "hey, um", "okay uh"])
def test_hesitation_before_wake_name_arms_and_keeps_followups(prefix):
    h = harness()
    assert h.hear(f"{prefix} computer", 10)["reason"] == "armed"
    assert h.hear("um open calculator", 11)["plan"] == [{"action": "open_app", "app": "Calculator"}]


@pytest.mark.parametrize("text", ["um open calculator", "we said computer open calculator",
                                 "I think computer open calculator", "um"])
def test_hesitation_never_authorizes_an_inactive_session(text):
    h = harness()
    assert h.hear(text, 10)["reason"] == "ignored"
    assert not h.calls


@pytest.mark.parametrize("verb,action,payload_key", [("type", "type_text", "text"),
                                                   ("search for", "web_search", "query")])
def test_filler_inside_literal_payload_is_preserved(verb, action, payload_key):
    h = harness()
    result = h.hear(f"um computer {verb} Um, Ah, the Computer!", 10)
    assert result["plan"][0]["action"] == action
    assert result["plan"][0][payload_key] == "Um, Ah, the Computer!"


def test_dictation_keeps_all_hesitations_and_wake_words():
    h = harness()
    h.hear("computer start dictation", 10)
    result = h.hear("Um, computer, hello.", 11)
    assert result["plan"] == [{"action": "type_text", "text": "Um, computer, hello. ", "submit": False}]
