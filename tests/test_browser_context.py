"""Session tab selection and input boundaries; all window/UI adapters are fake."""

from types import SimpleNamespace

import pytest

from voicebar.browser_context import (
    BrowserSelectionError,
    BrowserSession,
    site_tab_matches,
)
from voicebar.catalog import Catalog
from voicebar.executor import browser
from voicebar.executor.common import ExecutionError
from voicebar.executor.uia import matches, unique


def tab(name, automation_id=""):
    return {"name": name, "role": "tabitem", "automation_id": automation_id, "enabled": True}


def window(title="Other page", *, hwnd=10, pid=20):
    return SimpleNamespace(hwnd=hwnd, pid=pid, title=title + " - Google Chrome", exe=r"C:\Browser\chrome.exe")


def choose(session, active, rows):
    return session.choose(active, rows, hint="YouTube", hostname="www.youtube.com")


def test_unique_background_site_needs_no_history():
    rows = [tab("Docs"), tab("Music - YouTube")]
    assert choose(BrowserSession(), window("Docs"), rows) == rows[1]


def test_multiple_sites_choose_most_recent_observed_use():
    session = BrowserSession()
    rows = [tab("First - YouTube"), tab("Second - YouTube"), tab("Docs")]
    active = window("First - YouTube")
    session.observe(active, rows)
    active.title = "Second - YouTube - Google Chrome"
    session.observe(active, rows)
    active.title = "Docs - Google Chrome"
    assert choose(session, active, rows) == rows[1]


def test_caption_recency_merges_only_after_unique_live_tab_inventory():
    session = BrowserSession()
    active = window("First - YouTube")
    session.observe_caption(active)
    active.title = "Second - YouTube - Google Chrome"
    session.observe_caption(active)
    active.title = "Docs - Google Chrome"
    rows = [tab("First - YouTube"), tab("Second - YouTube"), tab("Docs")]
    assert choose(session, active, rows) == rows[1]


def test_caption_history_does_not_make_duplicate_live_names_identifiable():
    session = BrowserSession()
    active = window("Music - YouTube")
    session.observe_caption(active)
    active.title = "Docs - Google Chrome"
    with pytest.raises(BrowserSelectionError, match="same identity"):
        choose(session, active, [tab("Music - YouTube", "one"), tab("Music - YouTube", "two")])


def test_caption_use_expires_even_when_other_tabs_keep_window_active():
    now = [0]
    session = BrowserSession(clock=lambda: now[0], ttl_seconds=10)
    active = window("First - YouTube")
    session.observe_caption(active)
    now[0] = 5
    active.title = "Docs - Google Chrome"
    session.observe_caption(active)
    now[0] = 11
    with pytest.raises(BrowserSelectionError, match="Which tab"):
        choose(session, active, [tab("First - YouTube"), tab("Second - YouTube"), tab("Docs")])


def test_multiple_unseen_site_tabs_ask_without_guessing():
    rows = [tab("First - YouTube"), tab("Second - YouTube")]
    with pytest.raises(BrowserSelectionError, match="Which tab") as error:
        choose(BrowserSession(), window(), rows)
    assert [row["name"] for row in error.value.candidates] == [row["name"] for row in rows]


@pytest.mark.parametrize("ids", [("", ""), ("first", "second")])
def test_duplicate_titles_are_ambiguous_even_with_distinct_ids(ids):
    rows = [tab("Music - YouTube", identity) for identity in ids]
    with pytest.raises(BrowserSelectionError, match="same identity"):
        choose(BrowserSession(), window("Music - YouTube"), rows)


@pytest.mark.parametrize("change", ["expired", "reused_hwnd", "cleared", "disappeared"])
def test_stale_observations_never_break_an_ambiguity(change):
    now = [0]
    session = BrowserSession(clock=lambda: now[0], ttl_seconds=10)
    rows = [tab("First - YouTube"), tab("Second - YouTube")]
    active = window("First - YouTube")
    session.observe(active, rows)
    active.title = "Docs - Google Chrome"
    if change == "expired":
        now[0] = 11
    elif change == "reused_hwnd":
        active.pid = 21
    elif change == "cleared":
        session.clear()
    else:
        session.observe(active, [rows[1]])
    with pytest.raises(BrowserSelectionError, match="Which tab"):
        choose(session, active, rows)


def test_window_memory_is_bounded_and_does_not_cross_windows():
    session = BrowserSession(max_windows=1)
    rows = [tab("First - YouTube"), tab("Second - YouTube")]
    first = window("First - YouTube")
    session.observe(first, rows)
    second = window("Docs", hwnd=11)
    session.observe(second, rows)
    first.title = "Docs - Google Chrome"
    with pytest.raises(BrowserSelectionError, match="Which tab"):
        choose(session, first, rows)


@pytest.mark.parametrize("title,expected", [
    ("YouTube", True), ("Music - YouTube", True), ("Music | YouTube", True),
    ("youtube.com", True), ("How to use YouTube", False), ("YouTubers", False),
    ("youtube.com.evil.example", False), ("notyoutube.com", False),
])
def test_site_matching_uses_exact_metadata_labels(title, expected):
    assert site_tab_matches(title, "YouTube", "www.youtube.com") is expected


@pytest.fixture
def context(monkeypatch):
    active = window("Docs")
    native = SimpleNamespace(items=[active], active=active.hwnd)
    native.foreground = lambda: native.active
    native.windows = lambda: list(native.items)
    native.is_window = lambda hwnd, pid: any((item.hwnd, item.pid) == (hwnd, pid) for item in native.items)
    native.default_browser = lambda scheme: active.exe
    catalog = Catalog(sites={"youtube": "https://www.youtube.com"})
    ctx = SimpleNamespace(native=native, owned=None, browser_session=BrowserSession(),
                          loaded=SimpleNamespace(catalog=catalog, site_titles={"youtube": "YouTube"}),
                          apps=SimpleNamespace(browser=lambda target: target.exe == active.exe),
                          last_app="Notepad", trace=[], saved=0, started=False)

    def guard(target):
        if not native.is_window(target.hwnd, target.pid):
            raise ExecutionError("window identity changed")
        if ctx.owned is not None and ctx.owned.get(target.hwnd) != target.pid:
            raise ExecutionError("unowned")

    ctx.guard = guard
    ctx.target = lambda: next(item for item in native.items if item.hwnd == native.active)
    ctx.begin = lambda: setattr(ctx, "started", True)
    ctx.save = lambda: setattr(ctx, "saved", ctx.saved + 1)

    def wait(predicate, description):
        if not predicate():
            raise ExecutionError("Unconfirmed: " + description)

    ctx.wait = wait

    class Automation:
        def __init__(self):
            self.rows = [tab("Docs"), tab("Music - YouTube")]
            self.before_click = None
            self.after_click = None
            self.before_find = None

        def find(self, hwnd, selector):
            if self.before_find:
                self.before_find()
            return [dict(row) for row in self.rows if matches(row, selector)]

        def browser_tabs(self, hwnd):
            return self.find(hwnd, {"role": "tabitem"})

        def click(self, hwnd, selector, check):
            row = unique(self.rows, selector)
            if self.before_click:
                self.before_click()
            check()
            ctx.trace.append(("select", row["name"]))
            active.title = row["name"] + " - Google Chrome"
            if self.after_click:
                self.after_click()
            return row

    ctx.automation = Automation()

    def press(context, target, keys):
        browser._foreground_guard(context, target)
        context.begin()
        context.trace.append(("keys", keys))
        if keys == "ctrl+w":
            title = browser.page_title(active.title)
            ctx.automation.rows = [row for row in ctx.automation.rows if row["name"] != title]
            active.title = ctx.automation.rows[0]["name"] + " - Google Chrome"

    monkeypatch.setattr(browser, "press_window", press)
    return ctx


def test_close_background_site_selects_exact_tab_then_confirms_removal(context):
    detail = browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == [("select", "Music - YouTube"), ("keys", "ctrl+w")]
    assert "confirmed" in detail
    assert context.automation.rows == [tab("Docs")]


def test_ambiguous_background_close_does_not_mutate(context):
    context.automation.rows.append(tab("Another - YouTube"))
    with pytest.raises(BrowserSelectionError, match="Which tab"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == []
    assert not context.started


@pytest.mark.parametrize("race", ["focus", "identity"])
def test_target_race_before_selection_never_sends_close(context, race):
    if race == "focus":
        context.automation.before_click = lambda: setattr(context.native, "active", 999)
    else:
        context.automation.before_click = lambda: context.native.items.clear()
    with pytest.raises(ExecutionError):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == []


def test_focus_loss_after_selection_never_sends_close(context):
    context.automation.after_click = lambda: setattr(context.native, "active", 999)
    with pytest.raises(ExecutionError, match="Foreground changed"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == [("select", "Music - YouTube")]


def test_wrong_selected_tab_never_sends_close(context):
    context.automation.after_click = lambda: setattr(context.native.items[0], "title", "Docs - Google Chrome")
    with pytest.raises(ExecutionError, match="selected browser tab"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == [("select", "Music - YouTube")]


def test_duplicate_title_appearing_after_selection_never_sends_close(context):
    context.automation.rows[1]["automation_id"] = "original"
    context.automation.after_click = lambda: context.automation.rows.append(tab("Music - YouTube", "new"))
    with pytest.raises(ExecutionError, match="became ambiguous"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == [("select", "Music - YouTube")]


def test_close_requires_target_disappearance_even_if_window_title_changed(context, monkeypatch):
    def press(ctx, target, keys):
        ctx.trace.append(("keys", keys))
        target.title = "Docs - Google Chrome"
    monkeypatch.setattr(browser, "press_window", press)
    with pytest.raises(ExecutionError, match="tab to disappear"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == [("select", "Music - YouTube"), ("keys", "ctrl+w")]


def test_scoped_context_refuses_unowned_browser_before_inspection(context):
    context.owned = {}
    context.automation.before_find = lambda: pytest.fail("must not inspect an unowned test target")
    with pytest.raises(ExecutionError, match="unowned"):
        browser.tab(context, {"op": "close", "site": "youtube"})
    assert context.trace == []


def test_plain_url_uses_new_tab_in_existing_foreground_default_browser(context, monkeypatch):
    sent = []
    def send(ctx, target, events):
        browser._foreground_guard(ctx, target)
        sent.extend(event[2] for event in events if event[3] == 4)
    monkeypatch.setattr(browser, "_send", send)
    context.browser_backend = SimpleNamespace(launch=lambda *args: pytest.fail("must reuse foreground"))
    detail = browser.open_url(context, {"url": "https://example.com/?x=A&b=2"})
    assert context.trace == [("keys", "ctrl+t"), ("keys", "ctrl+l"), ("keys", "enter")]
    assert "".join(map(chr, sent)) == "https://example.com/?x=A&b=2"
    assert "new tab" in detail
    assert context.last_app is None


@pytest.mark.parametrize("kind", ["tab", "nav", "url"])
def test_clear_browser_commands_never_initialize_or_query_uia(context, monkeypatch, kind):
    from voicebar.executor import uia
    monkeypatch.setattr(uia, "Automation", lambda: pytest.fail("clear command must not initialize UIA"))
    context.automation = None
    monkeypatch.setattr(browser, "_send", lambda *args: None)
    if kind == "tab":
        browser.tab(context, {"op": "new"})
    elif kind == "nav":
        browser.nav(context, {"op": "back"})
    else:
        browser.open_url(context, {"url": "https://example.com"})


def test_url_input_focus_change_stops_before_typing_or_enter(context, monkeypatch):
    original = browser.press_window
    def press(ctx, target, keys):
        original(ctx, target, keys)
        if keys == "ctrl+l":
            ctx.native.active = 999
    monkeypatch.setattr(browser, "press_window", press)
    context.input_backend = SimpleNamespace(modifiers_held=lambda: False,
                                            send=lambda events: pytest.fail("must not type"))
    with pytest.raises(ExecutionError, match="Foreground changed"):
        browser.open_url(context, {"url": "https://example.com"})
    assert context.trace == [("keys", "ctrl+t"), ("keys", "ctrl+l")]


@pytest.mark.parametrize("state,expected", [
    ("active", True), ("inactive", False), ("asleep", False),
    ("ambiguous", False), ("changed", False), ("nonbrowser", False),
])
def test_runtime_foreground_records_only_verified_active_browser_caption(monkeypatch, state, expected):
    from voicebar.executor import common
    from voicebar.ui.controller import RuntimeController

    active = window("Music - YouTube")
    catalog = Catalog(apps=["Google Chrome"])
    native = SimpleNamespace(foreground=lambda: active.hwnd, windows=lambda: [active],
                             is_window=lambda hwnd, pid: state != "changed")
    apps = SimpleNamespace(matches=lambda target, name: True,
                           name_for_window=lambda target: catalog.apps[0] if len(catalog.apps) == 1 else None,
                           browser=lambda target: state != "nonbrowser")
    if state == "ambiguous":
        catalog.apps.append("Other registration")
    contexts = []
    class Context:
        def __init__(self, *args, **kwargs):
            assert kwargs["persist"] is False
            contexts.append(self)
            self.native, self.apps, self.closed = native, apps, False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.closed = True
    monkeypatch.setattr(common, "Context", Context)
    runtime = object.__new__(RuntimeController)
    runtime._foreground_app = None
    runtime._screen_target = (999, 999)
    runtime.root, runtime.loaded, runtime._executor_settings = "unused", SimpleNamespace(catalog=catalog), {}
    runtime.session = SimpleNamespace(gate=SimpleNamespace(conversational=True, asleep=state == "asleep",
                                                          until=float("-inf") if state == "inactive" else float("inf")))
    captions = []
    runtime._browser_session = SimpleNamespace(observe_caption=captions.append)
    result = runtime._foreground()
    assert bool(captions) is expected
    if expected:
        assert captions == [active]
    assert result == (None if state in {"ambiguous", "changed"} else "Google Chrome")
    assert runtime._screen_target is None
    assert len(contexts) == 1 and contexts[0].closed
