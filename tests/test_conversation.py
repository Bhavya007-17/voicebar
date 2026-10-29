"""Session, continuation, and screen routing checks without desktop effects."""

from types import SimpleNamespace

import pytest

from voicebar.catalog import Catalog, CatalogError, LoadedCatalog
from voicebar.conversation import ConversationGate
from voicebar.gate import Gate
from voicebar.listening import Chimes, GateSession, configured_gate
from voicebar.parser import parse
from voicebar.screen import requested_label


def catalog():
    return Catalog(apps=["Notepad", "Calculator", "Google Chrome"],
                   aliases={"chrome": "Google Chrome"},
                   browser_apps=("Google Chrome",),
                   sites={"youtube": "https://www.youtube.com"},
                   engines={"google": "https://google.com/search?q={q}"})


def harness(**kwargs):
    now, calls, resets = [10.0], [], []
    gate = ConversationGate(catalog(), ["computer"])

    def execute(plan):
        calls.extend(plan)
        return [{"ok": True}]

    session = GateSession(gate, execute=execute, clock=lambda: now[0],
                          chimes=Chimes(play=lambda: None),
                          context_reset=lambda: resets.append(True), **kwargs)

    def hear(text, at):
        now[0] = at
        session.tick(at)  # the capture controller expires idle sessions before queuing speech
        session.speech(at)
        return session.hear(text, start=at, end=at, speech_end=at,
                            generation=session.generation)

    return SimpleNamespace(gate=gate, session=session, hear=hear, now=now,
                           calls=calls, resets=resets)


def test_wake_once_then_commands_until_two_minutes_of_silence():
    h = harness()
    assert h.hear("open calculator", 10)["reason"] == "ignored"
    assert h.hear("computer", 11)["reason"] == "armed"
    assert h.gate.until == 131
    assert h.hear("open calculator", 100)["reason"] == "run"
    assert h.gate.until == 220
    assert h.hear("maximize it", 219)["plan"][0]["target"] == "Calculator"
    assert h.hear("close it", 340)["reason"] == "ignored"
    assert h.gate.last_app is None and h.resets


def test_speech_extends_only_an_existing_session_and_remarks_do_not_act():
    h = harness()
    h.session.speech(100)
    assert h.gate.until == float("-inf")
    h.hear("computer", 101)
    assert h.hear("I was talking about my homework", 200)["plan"] is None
    assert h.gate.until == 320 and not h.calls


def test_split_search_keeps_raw_query_and_does_not_call_the_model():
    model_calls = []
    h = harness(resolver=lambda *args: model_calls.append(args))
    h.hear("computer", 10)
    assert h.hear("Search up.", 11)["reason"] == "pending"
    result = h.hear("High Five!", 13)
    assert result["plan"] == [{"action": "web_search", "engine": "google", "query": "High Five!"}]
    assert not model_calls


@pytest.mark.parametrize("change", ["expired", "cancel", "wake", "off", "lock", "new_command"])
def test_pending_search_never_survives_cancellation_or_a_new_request(change):
    h = harness()
    h.hear("computer search up", 10)
    at = 11
    if change == "expired":
        at = 15
    elif change == "cancel":
        assert h.hear("never mind", 11)["reason"] == "cancelled"
    elif change == "wake":
        h.hear("computer", 11)
    elif change == "off":
        h.session.control(enabled=False)
        h.session.control(enabled=True)
    elif change == "lock":
        h.session.control(locked=True)
        h.session.control(locked=False)
    elif change == "new_command":
        assert h.hear("open notepad", 11)["reason"] == "run"
    result = h.hear("High five.", at)
    assert result["plan"] is None
    assert not any(action["action"] == "web_search" for action in h.calls)


def test_typing_prefix_is_not_retained_across_unbound_windows():
    h = harness()
    assert h.hear("computer type", 10)["plan"] is None
    assert h.gate.pending is None
    assert h.hear("Sensitive text", 11)["plan"] is None


def test_local_fallback_receives_authorized_followup_but_not_inactive_speech():
    model_calls = []

    def resolve(text, cat, context):
        model_calls.append(text)
        return [{"action": "open_app", "app": "Calculator"}]

    h = harness(resolver=resolve)
    assert h.hear("get calculator going", 10)["plan"] is None
    h.hear("computer", 11)
    assert h.hear("get calculator going", 12)["tier"] == "tier2"
    assert model_calls == ["computer get calculator going"]


def test_screen_resolution_never_runs_before_activation_and_precedes_model():
    observed, models = [], []

    def screen(text):
        observed.append(text)
        return [{"action": "app_action", "app": "active", "name": "click_role:hyperlink:Calendar"}]

    h = harness(grounded_resolver=screen, resolver=lambda *args: models.append(args))
    assert h.hear("open calendar", 10)["plan"] is None
    h.hear("computer", 11)
    assert h.hear("open calendar", 12)["tier"] == "screen"
    assert observed == ["open calendar"] and not models


def test_capture_revocation_during_screen_lookup_discards_its_result():
    h = harness()

    def screen(text):
        h.session.control(enabled=False)
        return [{"action": "app_action", "app": "active", "name": "click:Calendar"}]

    h.session.grounded_resolver = screen
    h.hear("computer", 10)
    assert h.hear("open calendar", 11)["plan"] is None
    assert not h.calls


@pytest.mark.parametrize("tier", ["screen", "model"])
def test_expiry_during_resolution_revokes_the_result(tier):
    h = harness()

    def late(*args):
        h.now[0] += 121
        return [{"action": "open_app", "app": "Notepad"}]

    if tier == "screen":
        h.session.grounded_resolver = late
    else:
        h.session.resolver = late
    h.hear("computer", 10)
    result = h.hear("open calendar", 11)
    assert result["reason"] == "stale_or_closed"
    assert result["plan"] is None and not h.calls
    assert h.gate.until == float("-inf") and h.resets


def test_screen_ambiguity_cannot_fall_through_to_model_selection():
    models = []

    def ambiguous(text):
        raise RuntimeError("Two Calendar links; specify the target")

    h = harness(grounded_resolver=ambiguous, resolver=lambda *args: models.append(args))
    result = h.hear("computer open calendar", 10)
    assert result["plan"] is None and not h.calls and not models
    assert "Two Calendar" in result["detail"]


def test_click_command_can_use_screen_grounding_before_execution():
    observed = []
    def screen(text):
        observed.append(text)
        return [{"action": "app_action", "app": "active", "name": "click_role:button:Calendar"}]
    h = harness(grounded_resolver=screen)
    result = h.hear("computer click the calendar icon", 10)
    assert result["tier"] == "screen"
    assert observed == ["click the calendar icon"]
    assert h.calls == [{"action": "app_action", "app": "active", "name": "click_role:button:Calendar"}]


def test_screen_failure_cancels_parsed_click_instead_of_falling_back():
    def screen(text):
        raise RuntimeError("The screen controls changed")
    h = harness(grounded_resolver=screen)
    result = h.hear("computer click Calendar", 10)
    assert result["plan"] is None and not h.calls


def test_sleep_and_expiry_forget_temporary_app_context():
    h = harness()
    h.hear("computer open calculator", 10)
    assert h.gate.last_app == "Calculator"
    h.hear("stop listening", 11)
    assert h.gate.asleep and h.gate.last_app is None and h.resets
    assert h.hear("wake up", 12)["reason"] == "asleep"
    assert h.hear("computer wake up", 13)["reason"] == "run"


@pytest.mark.parametrize("text", ["type two plus three", "add two plus three", "calculate 2+3"])
def test_calculator_expressions_are_contextual_and_never_model_written(text):
    plan = parse(text, catalog(), active_app="Calculator")
    assert plan == [{"action": "app_action", "app": "Calculator",
                     "name": "calculate:" + text.split(" ", 1)[1]}]
    if text.startswith("type "):
        assert parse(text, catalog(), active_app="Notepad")[0]["text"] == "two plus three"
    else:
        assert parse(text, catalog(), active_app="Notepad") is None


def test_open_then_calculate_and_browser_tab_window_distinction():
    assert parse("open calculator and add 2 plus 3", catalog()) == [
        {"action": "open_app", "app": "Calculator"},
        {"action": "app_action", "app": "Calculator", "name": "calculate:2 plus 3"}]
    assert parse("open a Chrome tab", catalog(), active_app="Google Chrome") == [{"action": "tab", "op": "new"}]
    assert parse("close a Chrome tab", catalog(), active_app="Google Chrome") == [{"action": "tab", "op": "close"}]
    assert parse("open new browser window", catalog(), active_app="Google Chrome") == [
        {"action": "app_action", "app": "active", "name": "browser_window:new"}]


@pytest.mark.parametrize("text,label", [("can you open Calendar", "Calendar"),
                                        ("select the Assignment", "Assignment"),
                                        ("we should open Calendar", None), ("open", None)])
def test_screen_names_are_cut_from_complete_direct_requests(text, label):
    assert requested_label(text) == label


def test_opt_in_configuration_leaves_reference_contract_unchanged(tmp_path):
    loaded = LoadedCatalog(catalog(), [], {}, [])
    original = configured_gate(tmp_path, loaded, document={})
    assert isinstance(original, Gate) and not getattr(original, "conversational", False)
    enabled = configured_gate(tmp_path, loaded, document={"conversation": {"enabled": True}})
    assert isinstance(enabled, ConversationGate) and enabled.silence_seconds == 120
    for options in ({"enabled": "yes"}, {"silence_seconds": -1}, {"unknown": True}):
        with pytest.raises(CatalogError):
            configured_gate(tmp_path, loaded, document={"conversation": options})
