"""Contract for the gate: Gate.hear(text, now) -> Decision(plan, reason).

reason is one of: run, armed, ignored, unresolved, asleep.
plan is the list of actions to execute, or None. `now` is seconds (monotonic).
Never edit an expectation to make a test pass; if one looks wrong, stop and ask.
"""
import pytest

from test_parser_contract import (CATALOG, CHROME, FUSION, app, close, media, mode,
                                  search, typed, win)
from voicebar.gate import Gate

PINNED = [CHROME, FUSION, "Claude", "Word", "Notepad"]


def gate(mode_="name"):
    return Gate(CATALOG, names=["computer"], mode=mode_, followup_s=8.0, open_apps=PINNED)


def heard(g, text, now):
    d = g.hear(text, now)
    return d.plan, d.reason


# --- name mode (default) ---------------------------------------------------

def test_unnamed_speech_is_ignored():
    g = gate()
    assert heard(g, "open chrome", 0) == (None, "ignored")


@pytest.mark.parametrize("text", [
    "computer open chrome", "Computer, open Chrome.", "hey computer open chrome",
    "Okay computer, open chrome",
])
def test_named_command_runs(text):
    assert heard(gate(), text, 0) == ([app(CHROME)], "run")


def test_name_must_come_first():
    g = gate()
    assert heard(g, "my computer is slow", 0) == (None, "ignored")
    assert heard(g, "open chrome computer", 0) == (None, "ignored")


def test_followup_window_needs_no_name_and_carries_the_app():
    g = gate()
    assert heard(g, "computer open fusion", 0) == ([app(FUSION)], "run")
    assert heard(g, "make it full screen", 5) == ([win("maximize", FUSION)], "run")
    # each run restarts the 8 s window: 5 + 8 = 13
    assert heard(g, "move it to the other screen", 12) == (
        [win("move_monitor", FUSION, monitor="next")], "run")
    assert heard(g, "minimize it", 21) == (None, "ignored")


def test_name_alone_opens_the_window():
    g = gate()
    assert heard(g, "computer", 0) == (None, "armed")
    assert heard(g, "open chrome", 3) == ([app(CHROME)], "run")


def test_named_but_not_understood_is_unresolved():
    # "unresolved" is the only reason that may be handed to tier 2 later
    assert heard(gate(), "computer what's the weather like", 0) == (None, "unresolved")


def test_noise_inside_the_window_is_ignored_and_does_not_extend_it():
    g = gate()
    heard(g, "computer open chrome", 0)
    assert heard(g, "did you see the game last night", 7) == (None, "ignored")
    assert heard(g, "maximize it", 9) == (None, "ignored")


def test_guarded_commands_run_when_named():
    g = gate()
    assert heard(g, "computer type hello", 0) == ([typed("hello")], "run")
    assert heard(g, "computer close chrome", 20) == ([close(CHROME)], "run")


def test_sleep_and_wake():
    g = gate()
    assert heard(g, "computer go to sleep", 0) == ([mode("sleep")], "run")
    assert heard(g, "open chrome", 1) == (None, "asleep")          # no follow-up while asleep
    assert heard(g, "computer open chrome", 2) == (None, "asleep")
    assert heard(g, "wake up", 3) == (None, "asleep")              # wake needs the name
    assert heard(g, "computer wake up", 4) == ([mode("wake")], "run")
    assert heard(g, "open chrome", 5) == ([app(CHROME)], "run")


def test_dictation_types_everything_until_stopped():
    g = gate()
    assert heard(g, "computer start dictation", 0) == ([mode("dictation_on")], "run")
    assert heard(g, "Hello world.", 2) == ([typed("Hello world. ")], "run")
    assert heard(g, "open chrome", 4) == ([typed("open chrome ")], "run")
    assert heard(g, "stop dictation", 6) == ([mode("dictation_off")], "run")
    assert heard(g, "open chrome", 7) == ([app(CHROME)], "run")    # back to commands


# --- open mode (opt-in): safe commands run without the name ------------------

def test_open_mode_runs_safe_commands_unnamed():
    g = gate("open")
    assert heard(g, "open chrome", 0) == ([app(CHROME)], "run")
    assert heard(gate("open"), "pause the music", 0) == ([media("play_pause")], "run")


@pytest.mark.parametrize("text", [
    "type hello", "search for cats", "google it", "close chrome", "close it",
    "close this tab", "press enter", "copy that", "start dictation",
    "open chrome and type hello",          # one guarded step guards the whole plan
])
def test_open_mode_guarded_commands_need_the_name(text):
    assert heard(gate("open"), text, 0) == (None, "ignored")


@pytest.mark.parametrize("text", ["pause", "back", "save", "copy", "mute", "maximize"])
def test_open_mode_single_words_need_the_name(text):
    assert heard(gate("open"), text, 0) == (None, "ignored")


def test_open_mode_only_knows_pinned_apps():
    g = gate("open")
    assert heard(g, "open spotify", 0) == (None, "ignored")        # installed, not pinned
    assert heard(g, "computer open spotify", 20) == ([app("Spotify")], "run")


def test_open_mode_named_and_followup_behave_like_name_mode():
    g = gate("open")
    assert heard(g, "computer search for cats", 0) == ([search("cats")], "run")
    g = gate("open")
    assert heard(g, "open notepad", 0) == ([app("Notepad")], "run")
    assert heard(g, "type hello", 3) == ([typed("hello")], "run")              # inside the window
    assert heard(g, "close it", 6) == ([win("close", "Notepad")], "run")
    assert heard(g, "type hello", 30) == (None, "ignored")


CONVERSATION = [
    "open the door", "close your eyes", "start the car", "run the tests", "show me the money",
    "move on", "type of thing", "search me", "google it", "look up", "go to bed", "go to work",
    "go to the store", "open the mail", "pause", "back", "save", "copy", "go back to the start",
    "close it down", "open up about it", "start over", "run", "show me", "next", "sleep on it",
    "put it on the table", "move it over there", "send it to me", "bring it on",
    "play it again", "turn it down", "press on", "hit me", "tap the screen", "make it bigger",
    "make it work", "refresh my memory", "save it for later", "open a window",
    "close the tab on that", "scroll through it", "mute him", "skip it",
    "i'll open chrome later", "did you see the game last night", "what time is it",
]


@pytest.mark.parametrize("text", CONVERSATION)
@pytest.mark.parametrize("mode_", ["name", "open"])
def test_conversation_does_nothing(mode_, text):
    assert heard(gate(mode_), text, 0) == (None, "ignored")
