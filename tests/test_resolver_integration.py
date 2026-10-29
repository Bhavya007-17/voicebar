"""Tier-2 authorization and learning integration, with only in-process fakes."""

import json
from dataclasses import dataclass

import pytest

from voicebar.catalog import Catalog
from voicebar.gate import Gate
from voicebar.listening import Chimes, GateSession
from voicebar.parser import parse
from voicebar.resolver.base import Resolver

LOOSE = "get calculator going"
CALCULATOR = {"action": "open_app", "app": "Calculator"}


class Selector:
    def __init__(self, *, output=None, error=None, hook=None):
        self.calls = []
        self.output, self.error, self.hook = output, error, hook

    def select(self, request, timeout_s):
        self.calls.append((request, timeout_s))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        if self.output is not None:
            return self.output
        identifier = next(
            candidate.id
            for candidate in request.candidates
            if candidate.action == CALCULATOR
        )
        return json.dumps({"actions": [identifier]})


@dataclass
class Harness:
    session: GateSession
    backend: Selector
    calls: list
    rows: list
    now: list

    def hear(self, text, *, push_to_talk=False):
        at = self.now[0]
        return self.session.hear(
            text,
            start=at,
            end=at,
            speech_end=at,
            generation=self.session.generation,
            push_to_talk=push_to_talk,
        )


def make_session(*, catalog=None, mode="name", backend=None):
    catalog = catalog or Catalog(
        apps=["Notepad", "Calculator"],
        engines={"google": "https://www.google.com/search?q={q}"},
    )
    backend = backend or Selector()
    calls, rows, now = [], [], [10.0]

    def execute(plan):
        calls.extend(plan)
        return [{"ok": True} for _ in plan]

    session = GateSession(
        Gate(catalog, ["computer", "jarvis"], mode=mode, open_apps=["Notepad"]),
        execute=execute,
        log=rows.append,
        clock=lambda: now[0],
        resolver=Resolver(backend).resolve,
        chimes=Chimes(clock=lambda: now[0], play=lambda: None),
    )
    return Harness(session, backend, calls, rows, now)


def test_named_loose_command_selects_code_built_plan_and_commits_after_execution():
    harness = make_session()
    assert parse(LOOSE, harness.session.gate.catalog) is None
    row = harness.hear("Hey, Jarvis, " + LOOSE)
    assert row["reason"] == "run" and row["tier"] == "tier2"
    assert row["plan"] == [CALCULATOR] and harness.calls == [CALCULATOR]
    assert row["executed"] is True and row["execution_attempted"] is True
    assert harness.session.gate.last_app == "Calculator"
    assert harness.session.gate.until == 18
    request, budget = harness.backend.calls[0]
    assert request.text == LOOSE and 0 < budget <= 1.5
    assert {"gate_ms", "resolver_ms", "execution_ms", "total_ms"} <= row[
        "timings"
    ].keys()


@pytest.mark.parametrize("authorization", ["none", "open", "followup", "ptt"])
def test_nameless_loose_speech_never_reaches_selector(authorization):
    harness = make_session(mode="open" if authorization == "open" else "name")
    if authorization == "followup":
        harness.session.gate.until = 20
    row = harness.hear(LOOSE, push_to_talk=authorization == "ptt")
    assert row["plan"] is None and row["reason"] in {"ignored", "unresolved"}
    assert harness.backend.calls == [] and harness.calls == []


@pytest.mark.parametrize(
    "state", ["tier1", "armed", "asleep", "dictation", "off", "locked", "chime"]
)
def test_selector_is_bypassed_outside_named_unresolved_command(state):
    harness = make_session()
    text = "computer " + LOOSE
    expected = None
    if state == "tier1":
        text, expected = "computer open calculator", "run"
    elif state == "armed":
        text, expected = "computer", "armed"
    elif state == "asleep":
        harness.session.gate.asleep, expected = True, "asleep"
    elif state == "dictation":
        harness.session.gate.dictating, expected = True, "run"
    elif state == "off":
        harness.session.control(enabled=False)
        expected = "stale_or_closed"
    elif state == "locked":
        harness.session.control(locked=True)
        expected = "stale_or_closed"
    elif state == "chime":
        harness.session.chimes.intervals.append((9, 11))
        expected = "chime_overlap"
    row = harness.hear(text)
    assert row["reason"] == expected and harness.backend.calls == []
    if state == "dictation":
        assert harness.calls == [
            {"action": "type_text", "text": text + " ", "submit": False}
        ]
    elif state == "tier1":
        assert harness.calls == [CALCULATOR]
    else:
        assert harness.calls == []


def test_named_ptt_loose_command_still_has_explicit_spoken_authorization():
    harness = make_session()
    row = harness.hear("computer " + LOOSE, push_to_talk=True)
    assert row["reason"] == "run" and row["tier"] == "tier2"
    assert len(harness.backend.calls) == 1 and harness.calls == [CALCULATOR]


@pytest.mark.parametrize("control", [{"enabled": False}, {"locked": True}])
def test_capture_revocation_during_selection_cancels_plan_before_executor(control):
    harness = make_session()
    harness.session.gate.last_app = "Notepad"
    harness.backend.hook = lambda: harness.session.control(**control)
    row = harness.hear("computer " + LOOSE)
    assert len(harness.backend.calls) == 1
    assert row["reason"] == "stale_or_closed" and row["plan"] is None
    assert harness.calls == [] and harness.session.gate.last_app == "Notepad"
    assert harness.session.gate.until == float("-inf")
    assert not harness.session.capture_allowed


@pytest.mark.parametrize(
    "backend",
    [
        Selector(output='{"actions":["none"]}'),
        Selector(output=""),
        Selector(error=ConnectionError("fake optional server is stopped")),
        Selector(error=RuntimeError("fake backend failed")),
    ],
    ids=["none", "empty", "offline", "raising"],
)
def test_unresolved_fallback_preserves_gate_state_and_does_not_execute(backend):
    harness = make_session(backend=backend)
    harness.session.gate.last_app = "Notepad"
    harness.session.gate.until = 12
    before = harness.session._snapshot()
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "unresolved" and row["tier"] == "tier2"
    assert row["plan"] is None and harness.calls == []
    assert harness.session._snapshot() == before


def test_unexpected_resolver_callable_error_is_also_fail_closed():
    harness = make_session()

    def broken(*args):
        raise ValueError("fake integration error")

    harness.session.resolver = broken
    before = harness.session._snapshot()
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "unresolved" and row["plan"] is None
    assert harness.calls == [] and harness.session._snapshot() == before


def test_failed_tier_two_execution_does_not_commit_context_or_followup():
    harness = make_session()
    harness.session.execute = lambda plan: [
        {"ok": False, "detail": "fake launch failed"}
    ]
    before = harness.session._snapshot()
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "run" and row["tier"] == "tier2"
    assert row["executed"] is False and harness.session._snapshot() == before


def test_stopped_selector_does_not_affect_tier_one_commands():
    harness = make_session(
        backend=Selector(error=ConnectionError("fake server stopped"))
    )
    row = harness.hear("computer open calculator")
    assert row["reason"] == "run" and row["tier"] == "tier1"
    assert harness.calls == [CALCULATOR] and harness.backend.calls == []
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "unresolved" and row["plan"] is None
    assert harness.calls == [CALCULATOR] and len(harness.backend.calls) == 1


def test_learned_alias_is_exact_and_returns_a_copy_without_backend():
    catalog = Catalog(apps=["Calculator"], command_aliases={LOOSE: [CALCULATOR]})
    harness = make_session(catalog=catalog)
    row = harness.hear("computer GET calculator going!")
    assert row["reason"] == "run" and row["tier"] == "tier1"
    assert harness.calls == [CALCULATOR] and harness.backend.calls == []
    plan = parse(LOOSE, catalog)
    plan[0]["app"] = "Notepad"
    assert catalog.command_aliases[LOOSE] == [CALCULATOR]
    assert parse(LOOSE + " eventually", catalog) is None


def test_learned_payload_must_match_raw_transcript_exactly():
    typed = {"action": "type_text", "text": "Hello, WORLD!", "submit": False}
    phrase = "jot this down hello world"
    catalog = Catalog(command_aliases={phrase: [typed]})
    assert parse("jot this down Hello, WORLD!", catalog) == [typed]
    assert parse("jot this down hello, world!", catalog) is None
    assert parse("jot this down Hello WORLD", catalog) is None


def test_learned_alias_for_unpinned_app_is_blocked_in_open_mode():
    catalog = Catalog(
        apps=["Notepad", "Calculator"], command_aliases={LOOSE: [CALCULATOR]}
    )
    harness = make_session(catalog=catalog, mode="open")
    row = harness.hear(LOOSE)
    assert row["reason"] == "ignored" and row["plan"] is None
    assert harness.calls == [] and harness.backend.calls == []
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "run" and row["tier"] == "tier1"
    assert harness.calls == [CALCULATOR]


def test_learned_alias_for_pinned_app_runs_in_open_mode():
    notepad = {"action": "open_app", "app": "Notepad"}
    catalog = Catalog(
        apps=["Notepad", "Calculator"], command_aliases={"get notepad going": [notepad]}
    )
    harness = make_session(catalog=catalog, mode="open")
    row = harness.hear("get notepad going")
    assert row["reason"] == "run" and row["tier"] == "tier1"
    assert harness.calls == [notepad] and harness.backend.calls == []


def test_learned_noncommand_is_rejected_by_both_tiers():
    catalog = Catalog(apps=["Calculator"], noncommands={LOOSE})
    harness = make_session(catalog=catalog)
    row = harness.hear("computer " + LOOSE)
    assert row["reason"] == "unresolved" and row["plan"] is None
    assert harness.calls == [] and harness.backend.calls == []
