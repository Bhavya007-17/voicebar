"""Contract for the tier-1 parser: parse(utterance, catalog) -> list[dict] | None.

None means "not a command". Never edit an expectation to make a test pass;
if one looks wrong, stop and ask.
"""
import pytest

from voicebar.catalog import Catalog
from voicebar.parser import parse

CATALOG = Catalog(
    apps=[
        "Google Chrome", "Microsoft Edge", "Microsoft Teams", "Claude",
        "Autodesk Fusion", "Word", "Excel", "PowerPoint", "Notepad",
        "Calculator", "File Explorer", "Visual Studio Code", "Cursor",
        "Spotify", "Discord", "Settings", "Sticky Notes", "Terminal",
    ],
    # spoken phrase -> app name. Covers nicknames and what the STT mishears.
    aliases={
        "cloud": "Claude",
        "fusion 360": "Autodesk Fusion",
        "vs code": "Visual Studio Code",
        "browser": "Google Chrome",
        "files": "File Explorer",
    },
    # spoken phrase -> URL
    sites={
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "github": "https://github.com",
        "google notes": "https://keep.google.com",
    },
    # search engine -> URL template
    engines={
        "google": "https://www.google.com/search?q={q}",
        "youtube": "https://www.youtube.com/results?search_query={q}",
        "github": "https://github.com/search?q={q}",
    },
)

CHROME, FUSION = "Google Chrome", "Autodesk Fusion"
YOUTUBE = "https://www.youtube.com"


def app(name): return {"action": "open_app", "app": name}
def switch(name): return {"action": "switch_app", "app": name}
def close(name): return {"action": "close_app", "app": name}
def win(op, target="active", **kw): return {"action": "window", "op": op, "target": target, **kw}
def url(u): return {"action": "open_url", "url": u}
def search(q, engine="google"): return {"action": "web_search", "engine": engine, "query": q}
def tab(op, **kw): return {"action": "tab", "op": op, **kw}
def nav(op): return {"action": "nav", "op": op}
def typed(text, submit=False): return {"action": "type_text", "text": text, "submit": submit}
def press(keys): return {"action": "press", "keys": keys}
def scroll(direction, amount=None):
    d = {"action": "scroll", "dir": direction}
    return d if amount is None else {**d, "amount": amount}
def volume(op): return {"action": "volume", "op": op}
def media(op): return {"action": "media", "op": op}
def mode(op): return {"action": "mode", "op": op}


COMMANDS = [
    # --- apps: launch, switch, close -------------------------------------
    ("open chrome", [app(CHROME)]),
    ("Open Chrome.", [app(CHROME)]),
    ("please open chrome", [app(CHROME)]),
    ("can you open up chrome for me", [app(CHROME)]),
    ("open claude", [app("Claude")]),
    ("open cloud", [app("Claude")]),                      # alias for a mishearing
    ("launch fusion", [app(FUSION)]),
    ("open fusion 360", [app(FUSION)]),
    ("open word document", [app("Word")]),
    ("open a new word document", [app("Word")]),
    ("open the calculator app", [app("Calculator")]),
    ("open vs code", [app("Visual Studio Code")]),
    ("open note pad", [app("Notepad")]),                  # STT split one word in two
    ("open power point", [app("PowerPoint")]),
    ("open the browser", [app(CHROME)]),
    ("switch to chrome", [switch(CHROME)]),
    ("go to claude", [switch("Claude")]),
    ("close claude", [close("Claude")]),
    ("quit spotify", [close("Spotify")]),
    # --- chains ----------------------------------------------------------
    ("close claude, open chrome", [close("Claude"), app(CHROME)]),
    ("close claude and open chrome", [close("Claude"), app(CHROME)]),
    ("open chrome and fusion", [app(CHROME), app(FUSION)]),            # verb carries over
    ("open word and excel", [app("Word"), app("Excel")]),
    ("open spotify and google chrome", [app("Spotify"), app(CHROME)]),
    ("close word and excel", [close("Word"), close("Excel")]),
    ("maximize chrome and move it to the other screen",
     [win("maximize", CHROME), win("move_monitor", CHROME, monitor="next")]),
    ("open fusion, make it full screen", [app(FUSION), win("maximize", FUSION)]),
    ("Open Fusion, make it full screen, and move it to the other screen.",
     [app(FUSION), win("maximize", FUSION), win("move_monitor", FUSION, monitor="next")]),
    ("close claude open chrome", [close("Claude"), app(CHROME)]),      # no separator at all
    ("Close Claude. Open Chrome.", [close("Claude"), app(CHROME)]),
    ("open chrome type hello", [app(CHROME), typed("hello")]),
    ("open fusion full screen", [app(FUSION), win("maximize", FUSION)]),
    ("open fusion on the other screen",
     [app(FUSION), win("move_monitor", FUSION, monitor="next")]),
    # --- windows ---------------------------------------------------------
    ("maximize", [win("maximize")]),
    ("full screen", [win("maximize")]),
    ("make it full screen", [win("maximize")]),
    ("make chrome full screen", [win("maximize", CHROME)]),
    ("minimize chrome", [win("minimize", CHROME)]),
    ("restore the window", [win("restore")]),
    ("close this window", [win("close")]),
    ("close it", [win("close")]),
    ("move it to the other screen", [win("move_monitor", monitor="next")]),
    ("move this to another screen", [win("move_monitor", monitor="next")]),
    ("put chrome on the left monitor", [win("move_monitor", CHROME, monitor="left")]),
    ("move fusion to the second display", [win("move_monitor", FUSION, monitor="next")]),
    ("send it to the main screen", [win("move_monitor", monitor="primary")]),
    ("snap it to the right", [win("snap_right")]),
    ("snap chrome left", [win("snap_left", CHROME)]),
    # --- browser ---------------------------------------------------------
    ("open youtube", [url(YOUTUBE)]),
    ("open you tube", [url(YOUTUBE)]),
    ("open youtube on google tab", [url(YOUTUBE)]),
    ("open youtube in a new tab", [url(YOUTUBE)]),
    ("open youtube in chrome", [url(YOUTUBE)]),
    ("open google", [url("https://www.google.com")]),
    ("go to github", [url("https://github.com")]),
    ("open stripe dot com", [url("https://stripe.com")]),
    ("go to docs.python.org", [url("https://docs.python.org")]),
    ("open google notes", [url("https://keep.google.com")]),
    ("close google notes", [tab("close", site="google notes")]),
    ("open another tab", [tab("new")]),
    ("open a new tab", [tab("new")]),
    ("new tab", [tab("new")]),
    ("close tab", [tab("close")]),
    ("close this tab", [tab("close")]),
    ("next tab", [tab("next")]),
    ("previous tab", [tab("prev")]),
    ("reopen closed tab", [tab("reopen")]),
    ("go back", [nav("back")]),
    ("go forward", [nav("forward")]),
    ("refresh", [nav("reload")]),
    ("reload the page", [nav("reload")]),
    # --- search: the query is everything after the verb -------------------
    ("search for lofi hip hop", [search("lofi hip hop")]),
    ("search lofi hip hop", [search("lofi hip hop")]),
    ("search mujoco contact models up", [search("mujoco contact models")]),
    ("look up flood fill algorithm", [search("flood fill algorithm")]),
    ("google best ramen near me", [search("best ramen near me")]),
    ("search youtube for flood fill algorithm", [search("flood fill algorithm", "youtube")]),
    ("search for flood fill algorithm on youtube", [search("flood fill algorithm", "youtube")]),
    ("open chrome and search for cats and dogs", [app(CHROME), search("cats and dogs")]),
    # --- typing: the text is everything after the verb --------------------
    ("type hello world", [typed("hello world")]),
    ("Type Hello world.", [typed("Hello world")]),         # keeps case, drops final period
    ("type hello world and press enter", [typed("hello world", submit=True)]),
    ("type hello world then hit enter", [typed("hello world", submit=True)]),
    ("type open chrome and close it", [typed("open chrome and close it")]),
    ("open notepad and type meeting at 3 pm, bring the laptop",
     [app("Notepad"), typed("meeting at 3 pm, bring the laptop")]),
    ("type how are you?", [typed("how are you?")]),          # keeps ? and !
    ("write dear professor", [typed("dear professor")]),
    # --- keys ------------------------------------------------------------
    ("press enter", [press("enter")]),
    ("hit escape", [press("esc")]),
    ("press control s", [press("ctrl+s")]),
    ("press control plus s", [press("ctrl+s")]),
    ("press alt tab", [press("alt+tab")]),
    ("press control shift t", [press("ctrl+shift+t")]),
    ("press f 11", [press("f11")]),
    ("select all", [press("ctrl+a")]),
    ("copy that", [press("ctrl+c")]),
    ("paste", [press("ctrl+v")]),
    ("undo", [press("ctrl+z")]),
    ("save the file", [press("ctrl+s")]),
    # --- scroll, volume, media -------------------------------------------
    ("scroll down", [scroll("down", "page")]),
    ("scroll up a little", [scroll("up", "small")]),
    ("scroll down a lot", [scroll("down", "large")]),
    ("scroll to the top", [scroll("top")]),
    ("go to the bottom", [scroll("bottom")]),
    ("volume up", [volume("up")]),
    ("turn the volume down", [volume("down")]),
    ("mute", [volume("mute")]),
    ("unmute", [volume("unmute")]),
    ("pause", [media("play_pause")]),
    ("pause the music", [media("play_pause")]),
    ("next song", [media("next")]),
    ("previous track", [media("prev")]),
    # --- modes -----------------------------------------------------------
    ("stop listening", [mode("sleep")]),
    ("go to sleep", [mode("sleep")]),
    ("start dictation", [mode("dictation_on")]),
    ("stop dictation", [mode("dictation_off")]),
    ("wake up", [mode("wake")]),
]

NOT_COMMANDS = [
    "",
    "open",
    "close",
    "type",
    "search",
    "scroll",
    "chrome",                                   # no verb
    "tab",
    "i'll open chrome later",
    "so then i told him to open the file",
    "can you close the door",
    "open the pod bay doors",
    "open the window",
    "close the door and open the window",
    "did you see the game last night",
    "we should move it to the other room",
    "move it to the other room",
    "what time is it",
    "play some music",
    "make it bigger",
    "open microsoft",                           # ambiguous: Edge or Teams
    "open chrome is slow today",                # leftover words: not a command
    "open chrome and tell me a joke",           # all or nothing
]


@pytest.mark.parametrize("utterance,expected", COMMANDS)
def test_command(utterance, expected):
    assert parse(utterance, CATALOG) == expected


@pytest.mark.parametrize("utterance", NOT_COMMANDS)
def test_not_a_command(utterance):
    assert parse(utterance, CATALOG) is None


def test_it_refers_to_the_app_from_the_previous_utterance():
    assert parse("make it full screen", CATALOG, last_app=FUSION) == [win("maximize", FUSION)]
    assert parse("move it to the other screen", CATALOG, last_app=FUSION) == [
        win("move_monitor", FUSION, monitor="next")]
    assert parse("close it", CATALOG, last_app=FUSION) == [win("close", FUSION)]
    # a new app in the same utterance takes over
    assert parse("open chrome and maximize it", CATALOG, last_app=FUSION) == [
        app(CHROME), win("maximize", CHROME)]
