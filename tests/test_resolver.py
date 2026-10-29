"""Stage 6 safety properties; no model, server, window, or external I/O."""

import json
import threading
import time

import pytest

from voicebar.catalog import Catalog
from voicebar.parser import parse
from voicebar.resolver.base import Resolver, decode_selection
from voicebar.resolver.candidates import (
    MAX_CANDIDATE_ID_CHARS,
    Candidate,
    ResolverRequest,
    build_request,
    strip_wake_name,
)

CATALOG = Catalog(
    apps=["Google Chrome", "Autodesk Fusion", "Notepad", "Calculator"],
    aliases={"browser": "Google Chrome", "fusion": "Autodesk Fusion"},
    sites={"youtube": "https://www.youtube.com", "github": "https://github.com"},
    engines={
        "google": "https://www.google.com/search?q={q}",
        "youtube": "https://www.youtube.com/results?search_query={q}",
    },
)
LOOSE = "computer make the current window take up all the space"


class FakeBackend:
    def __init__(self, output=None, action=None):
        self.output = output
        self.action = action
        self.calls = []

    def select(self, request, timeout_s):
        self.calls.append((request, timeout_s))
        if self.action is not None:
            identifier = next(
                candidate.id
                for candidate in request.candidates
                if candidate.action == self.action
            )
            return json.dumps({"actions": [identifier]})
        return self.output


@pytest.mark.parametrize(
    "output",
    [
        None,
        "",
        " ",
        "garbage",
        "```json\n{}\n```",
        "null",
        "[]",
        "true",
        "{}",
        '{"actions":[]}',
        '{"actions":["none"]}',
        '{"actions":["c000","none"]}',
        '{"actions":["unknown"]}',
        '{"actions":[false]}',
        '{"actions":[0]}',
        '{"actions":[null]}',
        '{"actions":[{"action":"close_app","app":"Calculator"}]}',
        '{"actions":"c000"}',
        '{"actions":["c000"],"extra":false}',
        '{"actions":["none"],"actions":["c000"]}',
        '{"actions":["c000","c000","c000","c000","c000"]}',
        '{"actions":[NaN]}',
        '{"actions":[Infinity]}',
        '{"actions":["c000"]} trailing',
        '{"actions":["c000"]}\n{}',
        "[" * 2000 + "]" * 2000,
        "x" * (16 * 1024 + 1),
        '{"actions":["' + "\u00e9" * 9000 + '"]}',
        17,
        b'{"actions":["c000"]}',
    ],
    ids=lambda value: "malformed-output",
)
def test_garbage_backend_fails_closed(output):
    backend = FakeBackend(output)
    assert Resolver(backend).resolve(LOOSE, CATALOG) is None
    assert len(backend.calls) == 1


def test_backend_error_never_escapes():
    class Broken:
        def select(self, request, timeout_s):
            raise RuntimeError("fake transport failed")

    assert Resolver(Broken()).resolve(LOOSE, CATALOG) is None


@pytest.mark.parametrize(
    "text,context",
    [
        ("make this window larger", None),
        ("I told computer to make this window larger", None),
        ("make this window larger", {"named": True}),
        ("computer make this window larger", {"names": ["jarvis"]}),
        ("computerized make this window larger", None),
        ("computer", None),
        ("", None),
        ("computer " + "x" * 4096, None),
        ("computer " + "word " * 41, None),
    ],
)
def test_backend_never_sees_unnamed_or_unbounded_speech(text, context):
    backend = FakeBackend('{"actions":["c000"]}')
    assert Resolver(backend).resolve(text, CATALOG, context) is None
    assert backend.calls == []


def test_configured_wake_synonyms_and_greetings_preserve_raw_text():
    assert (
        strip_wake_name(
            "Hey, Jarvis: write Hello,  WORLD?!", {"names": {"computer", "jarvis"}}
        )
        == "write Hello,  WORLD?!"
    )
    assert (
        strip_wake_name("Okay Voice Bar, take this away", {"names": ["voice bar"]})
        == "take this away"
    )


def test_known_tier_one_never_calls_backend():
    backend = FakeBackend('{"actions":["c000"]}')
    assert Resolver(backend).resolve("computer open calculator", CATALOG) is None
    assert backend.calls == []


def test_complete_selected_plan_is_validated_and_copied():
    action = {"action": "window", "op": "maximize", "target": "active"}
    backend = FakeBackend(action=action)
    result = Resolver(backend).resolve(LOOSE, CATALOG)
    assert result == [action]
    result[0]["op"] = "close"
    assert any(
        candidate.action == action for candidate in backend.calls[0][0].candidates
    )
    assert 0 < backend.calls[0][1] <= 1.5


def test_backend_cannot_rewrite_executable_candidate_fields():
    class Mutating:
        def select(self, request, timeout_s):
            chosen = next(
                candidate
                for candidate in request.candidates
                if candidate.action["action"] == "window"
            )
            chosen.action.clear()
            chosen.action.update({"action": "open_app", "app": "Made up app"})
            return json.dumps({"actions": [chosen.id]})

    assert Resolver(Mutating()).resolve(LOOSE, CATALOG) == [
        {"action": "window", "op": "maximize", "target": "active"}
    ]


def test_ordered_selection_has_no_model_authored_action_values():
    request = build_request("throw fusion on the big screen and blow it up", CATALOG)
    expected = [
        {"action": "open_app", "app": "Autodesk Fusion"},
        {
            "action": "window",
            "op": "move_monitor",
            "target": "Autodesk Fusion",
            "monitor": "next",
        },
        {"action": "window", "op": "maximize", "target": "Autodesk Fusion"},
    ]
    identifiers = [
        next(
            candidate.id
            for candidate in request.candidates
            if candidate.action == action
        )
        for action in expected
    ]
    assert decode_selection(json.dumps({"actions": identifiers}), request) == expected


def test_schema_and_prompt_describe_only_finite_complete_candidates():
    request = build_request("bring browser into view", CATALOG)
    assert request.schema["additionalProperties"] is False
    assert request.schema["required"] == ["actions"]
    assert request.schema["properties"]["actions"]["items"]["enum"] == [
        "none",
        *[candidate.id for candidate in request.candidates],
    ]
    assert all(
        candidate.action["action"] != "app_action" for candidate in request.candidates
    )
    assert (
        "Schema:" in request.prompt
        and "spans" in request.prompt
        and "engines" in request.prompt
    )
    assert "apps" in request.prompt and "sites" in request.prompt


def test_only_named_catalog_targets_are_offered():
    request = build_request("bring browser into view beside quantum editor", CATALOG)
    offered = {
        candidate.action["app"]
        for candidate in request.candidates
        if "app" in candidate.action
    }
    assert offered == {"Google Chrome"}
    assert all(
        candidate.action.get("target") in {None, "Google Chrome"}
        for candidate in request.candidates
    )
    unknown = build_request("bring quantum editor into view", CATALOG)
    assert not any("app" in candidate.action for candidate in unknown.candidates)
    assert {
        candidate.action["target"]
        for candidate in unknown.candidates
        if "target" in candidate.action
    } == {"active"}


def test_ambiguous_catalog_app_name_is_never_offered():
    catalog = Catalog(apps=["Google Chrome", "Microsoft Chrome"])
    request = build_request("bring chrome into view", catalog)
    assert not any("app" in candidate.action for candidate in request.candidates)


def test_incidental_fuzzy_phrase_does_not_name_an_app():
    catalog = Catalog(apps=["Windows Backup", "Bing"])
    request = build_request("bring this window back to normal size", catalog)
    assert not any("app" in candidate.action for candidate in request.candidates)
    assert {
        candidate.action["target"]
        for candidate in request.candidates
        if "target" in candidate.action
    } == {"active"}


def test_whole_words_and_split_exact_name_still_name_catalog_app():
    request = build_request("give note pad and chrome some room", CATALOG)
    assert {
        candidate.action["app"]
        for candidate in request.candidates
        if "app" in candidate.action
    } == {"Notepad", "Google Chrome"}


def test_prior_app_can_only_come_from_catalog():
    request = build_request(
        "make it take all the space", CATALOG, {"last_app": "Calculator"}
    )
    assert {
        candidate.action["target"]
        for candidate in request.candidates
        if "target" in candidate.action
    } == {"Calculator"}
    request = build_request(
        "make it take all the space", CATALOG, {"last_app": "Made up"}
    )
    assert {
        candidate.action["target"]
        for candidate in request.candidates
        if "target" in candidate.action
    } == {"active"}


def test_prior_app_pronoun_offers_foreground_open_and_close_actions():
    phrase = "bring it to the front"
    assert parse(phrase, CATALOG, last_app="Notepad") is None
    request = build_request(phrase, CATALOG, {"last_app": "Notepad"})
    offered = [candidate.action for candidate in request.candidates]
    for action in ("open_app", "switch_app", "close_app"):
        assert {"action": action, "app": "Notepad"} in offered
    backend = FakeBackend(action={"action": "switch_app", "app": "Notepad"})
    assert Resolver(backend).resolve("computer " + phrase, CATALOG,
                                     {"last_app": "Notepad"}) == [
        {"action": "switch_app", "app": "Notepad"}
    ]


@pytest.mark.parametrize("phrase,previous,expected", [
    ("bring calculator into view", "Notepad", {"Calculator"}),
    ("bring it to the front", "Missing", set()),
    ("bring this window forward", "Notepad", set()),
])
def test_prior_app_actions_require_unqualified_it_and_actual_catalog_entry(
    phrase, previous, expected
):
    request = build_request(phrase, CATALOG, {"last_app": previous})
    assert {
        candidate.action["app"] for candidate in request.candidates
        if "app" in candidate.action
    } == expected


def test_search_spans_keep_exact_raw_substrings_and_strip_engine_in_code():
    text = "could you look for MuJoCo  Contact Models?! on youtube"
    request = build_request(text, CATALOG)
    actions = [
        candidate.action
        for candidate in request.candidates
        if candidate.action["action"] == "web_search"
    ]
    assert {
        "action": "web_search",
        "engine": "youtube",
        "query": "MuJoCo  Contact Models?!",
    } in actions
    assert all(
        action["query"] in text and action["engine"] in CATALOG.engines
        for action in actions
    )


def test_type_spans_and_submit_are_cut_by_code():
    text = "could you jot down Hello,  WORLD?! and press enter"
    request = build_request(text, CATALOG)
    actions = [
        candidate.action
        for candidate in request.candidates
        if candidate.action["action"] == "type_text"
    ]
    assert {"action": "type_text", "text": "Hello,  WORLD?!", "submit": True} in actions
    assert all(action["text"] in text for action in actions)
    quoted = build_request('could you enter "Hello,  WORLD?!" into the field', CATALOG)
    assert any(
        candidate.action
        == {"action": "type_text", "text": "Hello,  WORLD?!", "submit": False}
        for candidate in quoted.candidates
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "find me MuJoCo Contact Models on google",
            {
                "action": "web_search",
                "engine": "google",
                "query": "MuJoCo Contact Models",
            },
        ),
        (
            "put these words in: Hello, Alex!",
            {"action": "type_text", "text": "Hello, Alex!", "submit": False},
        ),
        (
            "jot this down: Contact model, version B.",
            {
                "action": "type_text",
                "text": "Contact model, version B.",
                "submit": False,
            },
        ),
        (
            "use control shift t on the keyboard",
            {"action": "press", "keys": "ctrl+shift+t"},
        ),
    ],
)
def test_payload_and_key_introduction_variants_are_code_built(text, expected):
    request = build_request(text, CATALOG)
    assert expected in [candidate.action for candidate in request.candidates]


def test_explicit_active_window_remains_available_beside_named_app():
    request = build_request("fire up calculator and tuck this window away", CATALOG)
    assert {
        candidate.action["target"]
        for candidate in request.candidates
        if "target" in candidate.action
    } == {"active", "Calculator"}


def test_learned_noncommand_never_reaches_backend():
    catalog = Catalog(
        apps=CATALOG.apps, noncommands={"make the current window take up all the space"}
    )
    backend = FakeBackend('{"actions":["c000"]}')
    assert Resolver(backend).resolve(LOOSE, catalog) is None
    assert backend.calls == []


def test_schema_validation_is_required_even_for_known_id():
    candidate = Candidate("c000", {"action": "run_shell", "command": "anything"}, {})
    request = ResolverRequest("", "", {}, (candidate,))
    assert decode_selection('{"actions":["c000"]}', request) is None


def test_hard_timeout_bounds_call_and_prevents_accumulating_hung_workers():
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Hanging:
        def select(self, request, timeout_s):
            calls.append(threading.current_thread())
            entered.set()
            release.wait()
            return '{"actions":["c000"]}'

    resolver = Resolver(Hanging(), timeout_ms=60)
    started = time.monotonic()
    try:
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert time.monotonic() - started < 0.3
        assert entered.is_set()
        assert calls[0].daemon
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert len(calls) == 1
    finally:
        release.set()
        if calls:
            calls[0].join(timeout=1)


def test_hard_timeout_includes_candidate_construction(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers = []

    def hanging_build(*args, **kwargs):
        workers.append(threading.current_thread())
        entered.set()
        release.wait()

    monkeypatch.setattr("voicebar.resolver.base.build_request", hanging_build)
    backend = FakeBackend()
    try:
        started = time.monotonic()
        assert Resolver(backend, timeout_ms=40).resolve(LOOSE, CATALOG) is None
        assert time.monotonic() - started < 0.3
        assert entered.is_set() and backend.calls == []
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)


def test_loose_fixture_used_here_reaches_fallback():
    assert parse(strip_wake_name(LOOSE), CATALOG) is None


@pytest.mark.parametrize(
    "output,outcome",
    [
        (None, "not_sure"),
        ('{"actions":["none"]}', "not_sure"),
        ("garbage", "invalid_output"),
        ('{"actions":["unknown"]}', "invalid_output"),
        ('{"actions":["none"],"extra":true}', "invalid_output"),
        ("x" * (16 * 1024 + 1), "invalid_output"),
    ],
)
def test_outcome_distinguishes_abstention_from_invalid_output(output, outcome):
    resolver = Resolver(FakeBackend(output))
    assert resolver.resolve(LOOSE, CATALOG) is None
    assert resolver.last_outcome == outcome
    assert resolver.wait_idle(0)


@pytest.mark.parametrize("error,outcome", [(RuntimeError, "backend_error"), (TimeoutError, "timeout")])
def test_outcome_identifies_backend_failure(error, outcome):
    class Broken:
        def select(self, request, timeout_s):
            raise error("fake backend failure")

    resolver = Resolver(Broken())
    assert resolver.resolve(LOOSE, CATALOG) is None
    assert resolver.last_outcome == outcome
    assert resolver.wait_idle(0)


def test_actual_worker_completion_is_observable_without_reviving_late_plan():
    entered, release = threading.Event(), threading.Event()
    action = {"action": "window", "op": "maximize", "target": "active"}
    chosen = FakeBackend(action=action)

    class Delayed:
        def select(self, request, timeout_s):
            entered.set()
            release.wait()
            return chosen.select(request, timeout_s)

    resolver = Resolver(Delayed(), timeout_ms=20)
    try:
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert entered.is_set() and resolver.last_outcome == "timeout"
        assert not resolver.wait_idle(0)
        release.set()
        assert resolver.wait_idle(1)
        assert resolver.last_outcome == "timeout"
        assert resolver.resolve(LOOSE, CATALOG) == [action]
        assert resolver.last_outcome == "resolved"
    finally:
        release.set()
        assert resolver.wait_idle(1)


def test_busy_is_an_outcome_separate_from_a_model_abstention():
    release = threading.Event()

    class Delayed:
        def select(self, request, timeout_s):
            release.wait()
            return '{"actions":["none"]}'

    resolver = Resolver(Delayed(), timeout_ms=20)
    try:
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert resolver.last_outcome == "timeout"
        assert resolver.resolve(LOOSE, CATALOG) is None
        assert resolver.last_outcome == "busy"
        release.set()
        assert resolver.wait_idle(1)
        assert resolver.last_outcome == "busy"
    finally:
        release.set()
        assert resolver.wait_idle(1)


def test_semantic_ids_distinguish_action_families_and_targets():
    request = build_request("bring calculator into view then use the browser tab and page and next music track", CATALOG)
    choices = {candidate.id: candidate.action for candidate in request.candidates}
    assert choices["open_app.Calculator"] == {"action": "open_app", "app": "Calculator"}
    assert choices["window.snap_left.Calculator"] == {
        "action": "window", "op": "snap_left", "target": "Calculator",
    }
    assert choices["window.move_monitor.Calculator.next"] == {
        "action": "window", "op": "move_monitor", "target": "Calculator", "monitor": "next",
    }
    assert choices["nav.forward"] == {"action": "nav", "op": "forward"}
    assert choices["media.next"] == {"action": "media", "op": "next"}
    assert choices["tab.next"] == {"action": "tab", "op": "next"}


def test_semantic_payload_ids_select_only_code_cut_spans():
    payload = "Hello,  WORLD?!"
    request = build_request(f"could you jot down {payload} and press enter", CATALOG)
    schema_json = json.dumps(request.schema, ensure_ascii=False)
    references = json.loads(request.prompt.split("\nReferences: ", 1)[1].split("\nTranscript: ", 1)[0])
    assert payload not in schema_json
    assert "plan_action" not in request.prompt
    assert "candidates" not in references
    assert request.prompt.count('"spans":') == 1
    typed = next(candidate for candidate in request.candidates if candidate.action == {
        "action": "type_text", "text": payload, "submit": True,
    })
    assert typed.id == f"type_text.{typed.labels['span']}.submit"
    assert references["spans"][typed.labels["span"]] == {"source": "after_verb", "text": payload}
    assert decode_selection(json.dumps({"actions": [typed.id]}), request) == [typed.action]
    searched = build_request("look for MuJoCo Contact Models on youtube", CATALOG)
    search = next(candidate for candidate in searched.candidates if candidate.action == {
        "action": "web_search", "engine": "youtube", "query": "MuJoCo Contact Models",
    })
    assert search.id == f"web_search.youtube.{search.labels['span']}"
    assert "MuJoCo Contact Models" not in json.dumps(searched.schema)
    assert decode_selection(json.dumps({"actions": [search.id]}), searched) == [search.action]


def test_unusual_catalog_identities_have_unique_bounded_references_and_ids(monkeypatch):
    names = ["Lab.Editor", "Lab Editor", "x" * 200 + "A", "x" * 200 + "B", "!!!", "active"]
    sites = {"my.site": "https://one.example", "my site": "https://two.example"}
    catalog = Catalog(apps=names, sites=sites)
    monkeypatch.setattr("voicebar.resolver.candidates._named_targets", lambda *args: (names, list(sites)))
    request = build_request("bring the named applications into view", catalog)
    identifiers = [candidate.id for candidate in request.candidates]
    assert len(identifiers) == len(set(identifiers))
    assert max(map(len, identifiers)) <= MAX_CANDIDATE_ID_CHARS
    references = json.loads(request.prompt.split("\nReferences: ", 1)[1].split("\nTranscript: ", 1)[0])
    assert set(references["apps"].values()) == set(names)
    assert set(references["sites"].values()) == set(sites)
    assert "active" not in references["apps"]
    for candidate in request.candidates:
        assert decode_selection(json.dumps({"actions": [candidate.id]}), request) == [candidate.action]


@pytest.mark.parametrize("problem", ["extra", "unknown", "duplicate_field", "raw_payload"])
def test_semantic_selection_keeps_strict_validation_for_real_ids(problem):
    request = build_request("make this window enormous", CATALOG)
    identifier = "window.maximize.active"
    if problem == "extra":
        output = json.dumps({"actions": [identifier], "explanation": "maximize"})
    elif problem == "unknown":
        output = json.dumps({"actions": [identifier + ".invented"]})
    elif problem == "duplicate_field":
        output = '{"actions":["none"],"actions":[' + json.dumps(identifier) + "]}"
    else:
        output = json.dumps({"actions": [{"id": identifier, "text": "invented"}]})
    assert decode_selection(output, request) is None


@pytest.mark.parametrize(
    "text,families",
    [
        ("give the current window more room", {"window"}),
        ("bring my browser where I can see it", {"open_app", "switch_app", "close_app", "window"}),
        ("shuffle to the following tab", {"tab"}),
        ("let this webpage go forward", {"nav", "scroll"}),
        ("I need the speakers louder", {"volume"}),
        ("pause the music for a moment", {"media", "volume"}),
        ("start dictation please", {"mode"}),
        ("write Calculator window volume", {"type_text"}),
        ("search for Notepad screen music on youtube", {"web_search"}),
        ("bring up youtube for me", {"open_url", "tab"}),
        ("get my browser running then make the sound louder", {"open_app", "switch_app", "close_app", "window", "volume"}),
    ],
)
def test_generic_domain_cues_offer_only_relevant_action_families(text, families):
    request = build_request(text, CATALOG)
    assert {candidate.action["action"] for candidate in request.candidates} == families


def test_catalog_names_inside_payload_do_not_become_executable_app_choices():
    for text in (
        "write open Calculator and close Notepad",
        "look for Chrome window shortcuts on youtube",
    ):
        request = build_request(text, CATALOG, {"last_app": "Calculator"})
        assert all(candidate.action["action"] in {"type_text", "web_search"}
                   for candidate in request.candidates)
        assert not any("app" in candidate.action or "target" in candidate.action for candidate in request.candidates)
    typed = build_request("write search for Calculator", CATALOG)
    assert {candidate.action["action"] for candidate in typed.candidates} == {"type_text"}


def test_payload_verbs_inside_app_names_or_explanations_do_not_start_payloads():
    catalog = Catalog(apps=["Enter Studio", "Find Tools"])
    for text in ("bring Enter Studio to the front", "get Find Tools running", "show me how to type Hello"):
        request = build_request(text, catalog)
        assert not any(candidate.action["action"] in {"type_text", "web_search"} for candidate in request.candidates)


def test_only_first_payload_command_cuts_spans_and_its_contents_stay_opaque():
    request = build_request("jot down search for Close Window and press enter", CATALOG)
    assert {candidate.action["action"] for candidate in request.candidates} == {"type_text"}
    assert {candidate.action["text"] for candidate in request.candidates} == {
        "search for Close Window",
    }


@pytest.mark.parametrize("separator", [" and ", " then ", ", ", ". "])
def test_browser_tab_identity_does_not_offer_app_or_window_actions_without_separate_app_clause(separator):
    request = build_request("give me a fresh browser tab", CATALOG)
    assert {candidate.action["action"] for candidate in request.candidates} == {"tab"}
    request = build_request("get Notepad running" + separator + "give me a fresh browser tab", CATALOG)
    assert {candidate.action["app"] for candidate in request.candidates if "app" in candidate.action} == {"Notepad"}
    assert not any(candidate.action.get("target") == "Google Chrome" for candidate in request.candidates)
    assert any(candidate.action == {"action": "tab", "op": "new"} for candidate in request.candidates)


def test_generic_function_words_cannot_name_multiword_apps_but_aliases_still_can():
    catalog = Catalog(apps=["Get Started", "Notepad"], aliases={"welcome": "Get Started"})
    request = build_request("get notepad started", catalog)
    assert {candidate.action["app"] for candidate in request.candidates if "app" in candidate.action} == {"Notepad"}
    request = build_request("bring welcome into view", catalog)
    assert {candidate.action["app"] for candidate in request.candidates if "app" in candidate.action} == {"Get Started"}


@pytest.mark.parametrize("phrase", ["other screen", "another display", "other monitor"])
def test_other_monitor_choices_have_only_the_pointer_relative_canonical_direction(phrase):
    request = build_request("send it to the " + phrase, CATALOG, {"last_app": "Calculator"})
    moves = [candidate.action for candidate in request.candidates if candidate.action.get("op") == "move_monitor"]
    assert moves == [{"action": "window", "op": "move_monitor", "target": "Calculator", "monitor": "next"}]


def test_big_screen_and_explicit_directions_keep_monitor_choices_for_the_selector():
    for phrase in ("the big screen", "the primary display", "the monitor on the left"):
        request = build_request("send this window to " + phrase, CATALOG)
        moves = [candidate.action for candidate in request.candidates if candidate.action.get("op") == "move_monitor"]
        assert {action["monitor"] for action in moves} == {"next", "prev", "left", "right", "primary"}


def test_payload_references_exclude_command_words_and_preserve_quote_choices():
    request = build_request('enter the phrase "One  Two!" into the field', CATALOG)
    references = json.loads(request.prompt.split("\nReferences: ", 1)[1].split("\nTranscript: ", 1)[0])
    spans = list(references["spans"].values())
    assert {span["source"] for span in spans} == {"after_verb", "quoted"}
    assert {"source": "quoted", "text": "One  Two!"} in spans
    assert all(not span["text"].startswith("enter ") for span in spans)


def test_model_cannot_drop_an_explicit_later_command_clause():
    request = build_request("get Notepad running and make it fill this screen", CATALOG)
    assert request.minimum_actions == 2
    identifier = next(c.id for c in request.candidates if c.action == {
        "action": "open_app", "app": "Notepad",
    })
    assert decode_selection(json.dumps({"actions": [identifier]}), request) is None
    assert decode_selection('{"actions":["none"]}', request) is None


def test_literal_payload_conjunctions_are_not_additional_action_clauses():
    request = build_request("write open Notepad and close Calculator", CATALOG)
    assert request.minimum_actions == 1


def test_named_site_close_cannot_close_an_unrelated_current_tab():
    request = build_request("get rid of the github tab", CATALOG)
    actions = [candidate.action for candidate in request.candidates]
    assert {"action": "tab", "op": "close", "site": "github"} in actions
    assert {"action": "tab", "op": "close"} not in actions


def test_separate_unnamed_tab_clause_keeps_its_unqualified_close_choice():
    request = build_request("close github tab and get rid of the current tab", CATALOG)
    actions = [candidate.action for candidate in request.candidates]
    assert {"action": "tab", "op": "close", "site": "github"} in actions
    assert {"action": "tab", "op": "close"} in actions
