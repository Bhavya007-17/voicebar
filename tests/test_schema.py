"""Closed action-set boundaries and the untouched parser contract's expectations."""

from copy import deepcopy

import pytest
import test_parser_contract as contract

from voicebar.parser import KEYS, parse
from voicebar.schema import SchemaError, validate_action, validate_plan


@pytest.mark.parametrize("utterance,expected", contract.COMMANDS)
def test_every_contract_expectation_and_result_validates(utterance, expected):
    before = deepcopy(expected)
    assert validate_plan(expected) is expected
    assert expected == before
    actual = parse(utterance, contract.CATALOG)
    assert validate_plan(actual) is actual


def test_previous_utterance_contract_expectations_validate(monkeypatch):
    """Run the original assertions with validation around each parser result."""
    original_parse = contract.parse
    count = 0

    def validating_parse(*args, **kwargs):
        nonlocal count
        result = original_parse(*args, **kwargs)
        validate_plan(result)
        count += 1
        return result

    monkeypatch.setattr(contract, "parse", validating_parse)
    contract.test_it_refers_to_the_app_from_the_previous_utterance()
    assert count == 4


@pytest.mark.parametrize("action", [
    {"action": "app_action", "app": "Notepad", "name": "document_command"},
    {"action": "open_url", "url": "http://localhost:8080/path?q=hello#section"},
    {"action": "open_url", "url": "https://[::1]:443/"},
    {"action": "open_url", "url": "https://bücher.example/"},
    *[
        {"action": "window", "op": "move_monitor", "target": "active", "monitor": monitor}
        for monitor in ("next", "prev", "left", "right", "primary")
    ],
    *[
        {"action": "press", "keys": key}
        for key in sorted(set(KEYS.values()) | {"pageup", "pagedown", "f0", "f00", "f99", "f123"})
    ],
])
def test_valid_additional_shapes(action):
    before = deepcopy(action)
    assert validate_action(action) is action
    assert action == before


@pytest.mark.parametrize("utterance", ["press f 0", "press f 00", "press f 123"])
def test_function_key_numeric_suffix_matches_parser(utterance):
    validate_plan(parse(utterance, contract.CATALOG))


@pytest.mark.parametrize("action", [
    None, [], "open_app", 1, True, {}, {"action": []},
    {"action": "shell", "command": "anything"},
    {"action": "open_app", "app": "Notepad", "command": "anything"},
    {"action": "open_app", "app": "Notepad", 1: "anything"},
    {"action": "open_app"},
    {"action": "open_app", "app": " "},
    {"action": "switch_app", "app": False},
    {"action": "close_app", "app": ["Notepad"]},
    {"action": "app_action", "app": "Notepad"},
    {"action": "app_action", "app": "Notepad", "name": ""},
    {"action": "window", "op": "maximize"},
    {"action": "window", "op": "resize", "target": "active"},
    {"action": "window", "op": "maximize", "target": "active", "monitor": "next"},
    {"action": "window", "op": "move_monitor", "target": "active"},
    {"action": "window", "op": "move_monitor", "target": "active", "monitor": 1},
    {"action": "window", "op": "move_monitor", "target": "active", "monitor": "other"},
    {"action": "tab", "op": "new", "site": "github"},
    {"action": "tab", "op": "close", "site": None},
    {"action": "tab", "op": "unknown"},
    {"action": "nav", "op": "home"},
    {"action": "volume", "op": True},
    {"action": "media", "op": "play"},
    {"action": "mode", "op": "open"},
    {"action": "web_search", "engine": "google", "query": ""},
    {"action": "web_search", "engine": 1, "query": "something"},
    {"action": "type_text", "text": "hello"},
    {"action": "type_text", "text": "hello", "submit": 1},
    {"action": "type_text", "text": "hello", "submit": "false"},
    {"action": "type_text", "text": "", "submit": False},
    {"action": "press", "keys": "ctrl++s"},
    {"action": "press", "keys": "CTRL+s"},
    {"action": "press", "keys": "ctrl+shell"},
    {"action": "press", "keys": "enter;exit"},
    {"action": "press", "keys": ""},
    {"action": "scroll", "dir": "down"},
    {"action": "scroll", "dir": "up", "amount": 1},
    {"action": "scroll", "dir": "down", "amount": "huge"},
    {"action": "scroll", "dir": "top", "amount": "page"},
    {"action": "scroll", "dir": "bottom", "amount": None},
    {"action": "scroll", "dir": "left", "amount": "page"},
])
def test_invalid_shapes_fail_without_mutation(action):
    before = deepcopy(action)
    with pytest.raises(SchemaError):
        validate_action(action)
    assert action == before


@pytest.mark.parametrize("url", [
    "", "example.com", "/path", "file:///C:/file", "javascript:alert(1)",
    "https:///path", "https://", "https://example.com:99999/",
    "https://example.com:no/", "https://example.com:/", "https://example.com:0/",
    "https://user:secret@example.com/", "https://example.com/a b",
    "https://example.com/\npath", "https://example.com\\@evil.example/",
    "https://[invalid]/", "https://bad..example/", "https://-bad.example/",
    "https://example.com../",
])
def test_invalid_url_fails(url):
    with pytest.raises(SchemaError, match="url"):
        validate_action({"action": "open_url", "url": url})


@pytest.mark.parametrize("plan", [None, {}, (), [], True, [None]])
def test_invalid_plan_fails(plan):
    with pytest.raises(SchemaError):
        validate_plan(plan)


def test_plan_identifies_invalid_action_position():
    with pytest.raises(SchemaError, match="Action 2:.*amount"):
        validate_plan([
            {"action": "open_app", "app": "Notepad"},
            {"action": "scroll", "dir": "down"},
        ])
