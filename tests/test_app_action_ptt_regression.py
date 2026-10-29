"""Standalone PTT cannot replace an unresolved app action with a generic plan."""

from types import SimpleNamespace

import pytest

from voicebar import ptt
from voicebar.catalog import Catalog, LoadedCatalog


@pytest.fixture
def catalog():
    return Catalog(apps=["Claude", "Voice access", "Notepad"],
                   engines={"google": "https://example.com/?q={q}"},
                   app_actions={"Claude": {"voice": {
                       "say": ["start voice mode", "start voice input"],
                       "do": [{"click": {"name": "Dictate"}}],
                   }}})


def recognize_once(root, catalog, text, monkeypatch, *, foreground=None):
    # Capture and recognition are faked so this exercises only the PTT routing
    # boundary, with a deliberately unsafe selector answer as the tripwire.
    controls = SimpleNamespace(register=lambda: None, close=lambda: None,
                               wait_pressed=lambda stop: True, available=lambda: True,
                               held=lambda: True)
    backend = SimpleNamespace(name="fake", device="cpu", close=lambda: None)
    monkeypatch.setattr(ptt, "_hold", lambda *args, **kwargs: {
        "text": text, "status": "recognized", "released_at": None,
        "capture_closed": True, "executed": False, "execution_attempted": False,
    })
    selections, executions = [], []
    replacement = [{"action": "open_app", "app": "Voice access"}]

    def resolver(*args):
        selections.append(args)
        return replacement

    def executor(plan):
        executions.append(plan)
        return [{"ok": True} for _ in plan]

    report = ptt.listen_ptt(
        root, LoadedCatalog(catalog, [], {}, []), hotkeys=controls, microphone=object(),
        backend_factory=lambda *args, **kwargs: backend, state_reader=lambda: None,
        resolver=resolver, executor=executor, foreground_app=lambda: foreground,
        max_utterances=1, output_fn=lambda line: None,
    )
    return report.last_result, selections, executions


@pytest.mark.parametrize("foreground", ["Claude", "Notepad", None])
@pytest.mark.parametrize("text", [
    "computer start voice", "computer start voice unexpected mode",
    "computer open Claude and start voice dictate mode",
    "computer open Claude start voice delete everything",
])
def test_named_unresolved_app_phrase_cannot_reach_selector_or_executor(
        tmp_path, catalog, monkeypatch, foreground, text):
    result, selections, executions = recognize_once(
        tmp_path, catalog, text, monkeypatch, foreground=foreground)
    assert result["reason"] == "unresolved" and result["plan"] is None
    assert result["tier"] == "tier1"
    assert selections == [] and executions == []


def test_other_named_loose_requests_still_reach_optional_selector(tmp_path, catalog, monkeypatch):
    result, selections, executions = recognize_once(
        tmp_path, catalog, "computer bring Voice access to the front", monkeypatch)
    assert result["tier"] == "tier2" and result["reason"] == "run"
    assert len(selections) == 1 and executions == [result["plan"]]


@pytest.mark.parametrize("text,expected", [
    ("computer type start voice unexpected mode",
     {"action": "type_text", "text": "start voice unexpected mode", "submit": False}),
    ("computer search for start voice unexpected mode",
     {"action": "web_search", "engine": "google", "query": "start voice unexpected mode"}),
    ("computer start voice mode", {"action": "app_action", "app": "Claude", "name": "voice"}),
])
def test_literal_payloads_and_exact_app_actions_still_bypass_selector(
        tmp_path, catalog, monkeypatch, text, expected):
    result, selections, executions = recognize_once(
        tmp_path, catalog, text, monkeypatch, foreground="Claude")
    assert result["tier"] == "tier1" and result["plan"] == [expected]
    assert selections == [] and executions == [[expected]]
