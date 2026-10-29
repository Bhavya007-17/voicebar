"""Stage 7 grammar and guarded foreground integration, with no native windows."""

import pytest

from voicebar.app_gate import Stage7Gate as Gate
from voicebar.catalog import Catalog
from voicebar.gate import Gate as ReferenceGate
from voicebar.listening import Chimes, GateSession
from voicebar.parser import parse


@pytest.fixture
def catalog():
    return Catalog(
        apps=["Claude", "Google Chrome", "Word", "Notepad"],
        aliases={"chrome": "Google Chrome", "claude desktop": "Claude",
                 "microsoft word": "Word"},
        app_actions={
            "Claude": {
                "start_voice": {"say": ["start voice mode", "start voice input"],
                                "do": [{"click": {"name": "Start voice mode"}}],
                                "then": "sleep"},
                "new_chat": {"say": ["new chat"], "do": [{"keys": "ctrl+n"}]},
                "focus_message": {"say": ["focus the message box"],
                                  "do": [{"click": {"name": "Message"}}]},
            },
            "Google Chrome": {
                "new_window": {"say": ["new window"], "do": [{"keys": "ctrl+n"}]},
                "incognito": {"say": ["new incognito window"],
                              "do": [{"keys": "ctrl+shift+n"}]},
                "new_chat": {"say": ["new chat"], "do": [{"keys": "ctrl+t"}]},
            },
            "Word": {
                "blank_document": {"say": ["new blank document", "new document"],
                                   "do": [{"keys": "ctrl+n"}]},
            },
        },
    )


def action(app, name):
    return {"action": "app_action", "app": app, "name": name}


@pytest.mark.parametrize("spoken,app,name", [
    ("start voice mode", "Claude", "start_voice"),
    ("start voice input", "Claude", "start_voice"),
    ("new chat", "Claude", "new_chat"),
    ("focus the message box", "Claude", "focus_message"),
    ("new window", "Google Chrome", "new_window"),
    ("new incognito window", "Google Chrome", "incognito"),
    ("new blank document", "Word", "blank_document"),
    ("new document", "Word", "blank_document"),
])
def test_bare_app_phrases_require_the_foreground_app(catalog, spoken, app, name):
    assert parse(spoken, catalog, active_app=app) == [action(app, name)]
    assert parse(spoken, catalog, active_app="Notepad") is None
    assert parse(spoken, catalog, last_app=app) is None
    assert parse(spoken, catalog) is None


@pytest.mark.parametrize("spoken,app,name", [
    ("Start Voice Mode in CLAUDE desktop.", "Claude", "start_voice"),
    ("new chat in Claude", "Claude", "new_chat"),
    ("focus the message box in Claude", "Claude", "focus_message"),
    ("new incognito window in Chrome", "Google Chrome", "incognito"),
    ("new blank document in Microsoft Word", "Word", "blank_document"),
])
def test_explicit_app_suffix_works_from_any_foreground(catalog, spoken, app, name):
    assert parse(spoken, catalog, active_app="Notepad") == [action(app, name)]


@pytest.mark.parametrize("joiner", [" and ", ", ", " then ", ". ", " "])
def test_open_then_app_phrase_uses_app_in_same_utterance(catalog, joiner):
    assert parse("open Claude" + joiner + "start voice mode", catalog,
                 active_app="Notepad") == [
        {"action": "open_app", "app": "Claude"}, action("Claude", "start_voice")]


def test_context_is_restored_when_a_proposed_split_fails(catalog):
    assert parse("open Claude nonsense and new chat", catalog,
                 active_app="Notepad") is None
    assert parse("switch to Word and new blank document", catalog) == [
        {"action": "switch_app", "app": "Word"}, action("Word", "blank_document")]


def test_overlapping_app_phrases_are_foreground_scoped(catalog):
    assert parse("new chat", catalog, active_app="Claude") == [action("Claude", "new_chat")]
    assert parse("new chat", catalog, active_app="Google Chrome") == [
        action("Google Chrome", "new_chat")]
    assert parse("new chat", catalog) is None


def test_ambiguous_action_or_explicit_app_fails_closed(catalog):
    catalog.app_actions["Claude"]["other"] = {
        "say": ["new chat"], "do": [{"keys": "ctrl+t"}]}
    assert parse("new chat", catalog, active_app="Claude") is None
    catalog.apps += ["Claude Work", "Claude Personal"]
    assert parse("new chat in claud", catalog) is None


@pytest.mark.parametrize("spoken", [
    "start voice mode later", "new chat and tell me a joke",
    "we should start voice mode", "start voice mode in Notepad", "click",
])
def test_unknown_or_extra_words_do_not_make_a_partial_plan(catalog, spoken):
    assert parse(spoken, catalog, active_app="Claude") is None


def test_generic_click_preserves_the_raw_control_name(catalog):
    assert parse("click What's New?", catalog) == [action("active", "click:What's New?")]
    assert parse("click Start Voice Mode.", catalog) == [action("active", "click:Start Voice Mode")]


def test_typing_and_searching_swallow_app_phrases(catalog):
    assert parse("type new chat in Claude", catalog, active_app="Claude") == [
        {"action": "type_text", "text": "new chat in Claude", "submit": False}]
    assert parse("search for new chat", catalog, active_app="Claude") == [
        {"action": "web_search", "engine": "google", "query": "new chat"}]


@pytest.mark.parametrize("spoken,title", [
    ("switch to tab named Release Notes!", "Release Notes!"),
    ("switch to the tab named Project Apollo", "Project Apollo"),
    ("switch to tab Project Apollo", "Project Apollo"),
])
def test_chrome_tab_titles_are_cut_by_code_and_keep_case(catalog, spoken, title):
    expected = [action("Google Chrome", "switch_tab:" + title)]
    assert parse(spoken, catalog, active_app="Google Chrome") == expected
    assert parse(spoken + " in Chrome", catalog, active_app="Notepad") == expected
    assert parse(spoken, catalog, active_app="Notepad") is None


def test_chrome_tab_title_can_follow_open_in_same_utterance(catalog):
    assert parse("open Chrome and switch to tab named Project Apollo", catalog) == [
        {"action": "open_app", "app": "Google Chrome"},
        action("Google Chrome", "switch_tab:Project Apollo")]


@pytest.mark.parametrize("plan", [
    [action("Claude", "start_voice")],
    [action("Claude", "does_not_exist")],
    [action("active", "click:Delete")],
    [{"action": "open_app", "app": "Claude"}, action("Claude", "start_voice")],
])
def test_learned_plan_cannot_bypass_app_phrase_or_foreground_checks(catalog, plan):
    catalog.command_aliases["do the thing"] = plan
    assert parse("do the thing", catalog, active_app="Claude") is None


@pytest.mark.parametrize("spoken", ["new chat", "click Start voice mode",
                                   "start voice mode in Claude",
                                   "open Claude and start voice mode"])
def test_app_actions_are_guarded_in_open_mode_including_mixed_chains(catalog, spoken):
    gate = Gate(catalog, ["computer"], mode="open", open_apps=catalog.apps)
    assert gate.hear(spoken, 0, active_app="Claude").reason == "ignored"
    named = gate.hear("computer " + spoken, 1, active_app="Claude")
    assert named.reason == "run" and named.plan
    followup = gate.hear(spoken, 2, active_app="Claude")
    assert followup.reason == "run" and followup.plan


def make_session(catalog, foreground, result=None, dry_run=False):
    calls = []

    def execute(plan):
        calls.extend(plan)
        return [result or {"ok": True}]

    session = GateSession(
        Gate(catalog, ["computer"]), execute=execute,
        clock=lambda: 10.0, resolver=lambda *args: None,
        foreground_app=foreground, dry_run=dry_run,
        chimes=Chimes(clock=lambda: 10.0, play=lambda: None))
    return session, calls


def hear(session, text):
    return session.hear(text, start=8, end=9, speech_end=9,
                        generation=session.generation)


def test_live_session_queries_foreground_each_utterance(catalog):
    active = ["Claude"]
    session, calls = make_session(catalog, lambda: active[0])
    assert hear(session, "computer new chat")["reason"] == "run"
    active[0] = "Word"
    assert hear(session, "computer new chat")["reason"] == "unresolved"
    assert hear(session, "computer new document")["reason"] == "run"
    assert calls == [action("Claude", "new_chat"), action("Word", "blank_document")]


def test_unknown_foreground_blocks_bare_app_phrase(catalog):
    def unavailable():
        raise RuntimeError("foreground unknown")

    session, calls = make_session(catalog, unavailable)
    assert hear(session, "computer new chat")["reason"] == "unresolved"
    assert not calls
    assert hear(session, "computer new chat in Claude")["reason"] == "run"


def test_successful_voice_action_puts_voicebar_to_sleep_and_stops_tail(catalog):
    session, calls = make_session(catalog, lambda: "Claude")
    row = hear(session, "computer start voice mode and press enter")
    assert calls == [action("Claude", "start_voice")]
    assert row["state_after"] == "asleep" and row["post_action_mode"] == "sleep"
    assert row["skipped_actions"] == [{"action": "press", "keys": "enter"}]
    assert session.capture_allowed and session.gate.until == float("-inf")


def test_failed_voice_action_does_not_put_voicebar_to_sleep(catalog):
    session, calls = make_session(catalog, lambda: "Claude", {"ok": False})
    row = hear(session, "computer start voice mode")
    assert calls == [action("Claude", "start_voice")]
    assert row["state_after"] == "listening" and "post_action_mode" not in row
    assert session.gate.until == float("-inf")


def test_dry_run_simulates_post_action_sleep_without_executor(catalog):
    session, calls = make_session(catalog, lambda: "Claude", dry_run=True)
    row = hear(session, "computer start voice mode")
    assert not calls and row["state_after"] == "asleep"


def test_catalog_foreground_context_is_pure_and_explicit_argument_can_override(catalog):
    catalog.active_app = "Claude"
    assert parse("new chat", catalog) == [action("Claude", "new_chat")]
    assert parse("new chat", catalog, active_app="Notepad") is None


def test_stage7_gate_restores_catalog_scope_after_each_utterance(catalog):
    gate = Gate(catalog, ["computer"], mode="open", open_apps=catalog.apps)
    open_catalog = gate.open_catalog
    assert gate.hear("computer new chat", 0, active_app="Claude").plan == [
        action("Claude", "new_chat")]
    assert gate.catalog is catalog and gate.open_catalog is open_catalog
    assert catalog.active_app is None and open_catalog.active_app is None
    assert gate.hear("computer new chat", 1).plan is None


def test_stage7_gate_restores_catalog_scope_if_reference_gate_raises(catalog, monkeypatch):
    gate = Gate(catalog, ["computer"])
    open_catalog = gate.open_catalog

    def broken(*args):
        raise RuntimeError("reference failure")

    monkeypatch.setattr(ReferenceGate, "hear", broken)
    with pytest.raises(RuntimeError, match="reference failure"):
        gate.hear("computer new chat", 0, active_app="Claude")
    assert gate.catalog is catalog and gate.open_catalog is open_catalog
    assert catalog.active_app is None


def test_app_action_acceptance_preserves_last_app_in_plan_order(catalog):
    gate = Gate(catalog, ["computer"])
    gate._accept([action("Claude", "new_chat"), {"action": "open_app", "app": "Word"}], 0)
    assert gate.last_app == "Word"
    gate._accept([{"action": "open_app", "app": "Word"}, action("Claude", "new_chat")], 1)
    assert gate.last_app == "Claude"
