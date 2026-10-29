"""Browser handlers are tested with fake launch/window/input boundaries."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar.catalog import Catalog
from voicebar.executor import browser, execute
from voicebar.executor.common import ExecutionError


@pytest.fixture
def ctx():
    old = SimpleNamespace(hwnd=1, pid=11, exe=r"C:\Browser\chrome.exe", title="GitHub - Google Chrome")
    native = SimpleNamespace(items=[old], active=1, default=r"C:\Browser\chrome.exe")
    native.windows = lambda: native.items
    native.foreground = lambda: native.active
    native.default_browser = lambda scheme: native.default
    native.is_window = lambda hwnd, pid: any(w.hwnd == hwnd and w.pid == pid for w in native.items)

    def activate(hwnd, timeout):
        native.active = hwnd

    native.activate = activate
    catalog = Catalog(apps=["Google Chrome"], sites={"github": "https://github.com"},
                      engines={"google": "https://www.google.com/search?q={q}"})
    context = SimpleNamespace(native=native, apps=SimpleNamespace(browser=lambda w: True),
                              loaded=SimpleNamespace(catalog=catalog, site_titles={"github": "GitHub"}),
                              owned={1: 11}, starts=0, timeout=1, last_app="Notepad", saved=0,
                              launches=[], shell_urls=[])

    def guard(window):
        if context.owned is not None and context.owned.get(window.hwnd) != window.pid:
            raise ExecutionError("unowned")

    def adopt(window):
        if context.owned is not None:
            context.owned[window.hwnd] = window.pid

    def begin():
        context.starts += 1

    def save():
        context.saved += 1

    def wait(predicate, description):
        result = predicate()
        if not result:
            raise ExecutionError(f"Unconfirmed: {description}")
        return result

    def launch(executable, url, profile):
        context.launches.append((executable, url, profile))
        native.items.append(SimpleNamespace(hwnd=2, pid=22, exe=executable, title="New page"))

    context.guard, context.adopt, context.begin, context.save, context.wait = guard, adopt, begin, save, wait
    context.target = lambda: next(w for w in native.items if w.hwnd == native.active)
    context.browser_backend = SimpleNamespace(launch=launch, shell_open=context.shell_urls.append)
    return context


def test_default_browser_new_window_is_only_new_identity_and_is_adopted(ctx):
    detail = browser.open_url(ctx, {"url": "https://github.com"})
    assert ctx.launches == [(r"C:\Browser\chrome.exe", "https://github.com", None)]
    assert ctx.owned == {1: 11, 2: 22}
    assert ctx.native.active == 2 and ctx.last_app is None and "confirmed" in detail


def test_simulated_default_browser_identity_and_site_close(ctx, monkeypatch):
    """Preserve the browser scenario at fake boundaries; native tabs are unverified."""
    ctx.loaded.catalog.sites.update({"youtube": "https://www.youtube.com",
                                     "gmail": "https://mail.google.com"})
    ctx.loaded.site_titles.update({"youtube": "YouTube", "gmail": "Gmail"})
    ctx.apps.browser = lambda window: browser._same_exe(window.exe, ctx.native.default)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    previous = ctx.native.items[0]
    previous_title = previous.title
    inputs = []
    launch = ctx.browser_backend.launch

    def launch_youtube(executable, url, profile):
        launch(executable, url, profile)
        ctx.native.items[-1].title = "YouTube - Google Chrome"

    def press_owned_browser(context, window, keys):
        context.guard(window)
        assert context.native.is_window(window.hwnd, window.pid)
        assert context.native.foreground() == window.hwnd
        assert context.apps.browser(window)
        inputs.append((window.hwnd, keys))
        if keys == "ctrl+t":
            window.title = "New Tab - Google Chrome"
        elif keys == "ctrl+w" and window.title == "New Tab - Google Chrome":
            window.title = "YouTube - Google Chrome"
        elif keys == "ctrl+w":
            context.native.items.remove(window)
            context.native.active = previous.hwnd
        else:
            pytest.fail(f"Unexpected browser shortcut: {keys}")

    ctx.browser_backend.launch = launch_youtube
    monkeypatch.setattr(browser, "press_window", press_owned_browser)

    def run(action):
        result = execute([action], context=ctx)
        assert result[0]["ok"], result

    run({"action": "open_url", "url": ctx.loaded.catalog.sites["youtube"]})
    window = ctx.target()
    assert (window.hwnd, window.pid) not in baseline
    assert ctx.owned[window.hwnd] == window.pid
    assert ctx.apps.browser(window)
    assert any(word in window.title.casefold() for word in ("youtube", "youtube.com"))

    result = execute([{"action": "tab", "op": "close", "site": "gmail"}], context=ctx)
    assert not result[0]["ok"]
    assert "does not match" in result[0]["detail"]
    assert not inputs
    assert ctx.native.is_window(window.hwnd, window.pid)
    title = ctx.target().title
    run({"action": "tab", "op": "new"})
    assert ctx.target().title != title
    run({"action": "tab", "op": "close"})
    assert "youtube" in ctx.target().title.casefold()
    run({"action": "tab", "op": "close", "site": "youtube"})
    assert not ctx.native.is_window(window.hwnd, window.pid)
    assert inputs == [(window.hwnd, "ctrl+t"), (window.hwnd, "ctrl+w"), (window.hwnd, "ctrl+w")]
    assert ctx.native.is_window(previous.hwnd, previous.pid)
    assert previous.title == previous_title


def test_test_only_profile_is_forwarded(ctx, tmp_path):
    ctx.browser_user_data_dir = tmp_path
    browser.open_url(ctx, {"url": "https://github.com"})
    assert ctx.launches[0][2] == tmp_path


def test_preexisting_browser_is_never_adopted(ctx):
    ctx.browser_backend.launch = lambda *args: None
    with pytest.raises(ExecutionError, match="new default-browser window"):
        browser.open_url(ctx, {"url": "https://github.com"})
    assert ctx.owned == {1: 11}


def test_multiple_new_browser_windows_refuse_ambiguous_ownership(ctx):
    def launch(executable, *args):
        ctx.native.items.extend(SimpleNamespace(hwnd=i, pid=i + 20, exe=executable, title="Page")
                                for i in (2, 3))

    ctx.browser_backend.launch = launch
    with pytest.raises(ExecutionError, match="multiple candidate"):
        browser.open_url(ctx, {"url": "https://github.com"})
    assert ctx.owned == {1: 11}


def test_same_basename_different_executable_does_not_claim_ownership(ctx):
    def launch(*args):
        ctx.native.items.append(SimpleNamespace(hwnd=2, pid=22, exe=r"C:\Other\chrome.exe", title="Page"))

    ctx.browser_backend.launch = launch
    with pytest.raises(ExecutionError, match="new default-browser window"):
        browser.open_url(ctx, {"url": "https://github.com"})
    assert ctx.owned == {1: 11}


def test_unsupported_default_browser_refuses_scoped_fallback(ctx):
    ctx.native.default = r"C:\Browser\firefox.exe"
    with pytest.raises(ExecutionError, match="scoped URL launch refused"):
        browser.open_url(ctx, {"url": "https://github.com"})
    assert not ctx.shell_urls and ctx.starts == 0


def test_unsupported_default_browser_normal_runtime_uses_literal_url(ctx):
    ctx.owned = None
    ctx.native.default = r"C:\Browser\firefox.exe"
    browser.open_url(ctx, {"url": "https://example.com/?x=hello&y=world"})
    assert ctx.shell_urls == ["https://example.com/?x=hello&y=world"]


def test_search_url_encodes_raw_query_as_data(ctx):
    browser.web_search(ctx, {"engine": "google", "query": "A&B # 😀"})
    assert ctx.launches[0][1] == "https://www.google.com/search?q=A%26B+%23+%F0%9F%98%80"


def test_unknown_engine_refuses_without_launch(ctx):
    with pytest.raises(ExecutionError, match="not configured"):
        browser.web_search(ctx, {"engine": "missing", "query": "hello"})
    assert not ctx.launches


def test_launch_uses_fixed_argv_without_shell(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    browser.BrowserBackend().launch(r"C:\Browser\chrome.exe", "https://example.com/?q=&x=\"", tmp_path)
    argv, kwargs = calls[0]
    assert argv == [r"C:\Browser\chrome.exe", "--new-window",
                    f"--user-data-dir={Path(tmp_path).resolve()}", "https://example.com/?q=&x=\""]
    assert kwargs["shell"] is False


@pytest.mark.parametrize("title,hint,host,expected", [
    ("GitHub - Google Chrome", "GitHub", "github.com", True),
    ("github.com - Google Chrome", None, "github.com", True),
    ("www.github.com", None, "github.com", True),
    ("evilgithub.com", None, "github.com", False),
    ("github.com.evil.example", None, "github.com", False),
    ("GitHubbers", "GitHub", "github.com", False),
])
def test_site_title_hint_and_host_use_boundaries(title, hint, host, expected):
    assert browser._site_matches(title, hint, host) is expected


def test_close_site_refuses_mismatching_active_tab(ctx, monkeypatch):
    ctx.native.items[0].title = "Unrelated page"
    calls = []
    monkeypatch.setattr(browser, "press_window", lambda *args: calls.append(args))
    with pytest.raises(ExecutionError, match="does not match"):
        browser.tab(ctx, {"op": "close", "site": "github"})
    assert not calls


def test_close_site_requires_observed_change(ctx, monkeypatch):
    monkeypatch.setattr(browser, "press_window", lambda *args: None)
    with pytest.raises(ExecutionError, match="identical titles"):
        browser.tab(ctx, {"op": "close", "site": "github"})


def test_close_site_confirms_title_change(ctx, monkeypatch):
    calls = []

    def press(context, window, keys):
        calls.append((window.hwnd, keys))
        window.title = "Other page"

    monkeypatch.setattr(browser, "press_window", press)
    assert "confirmed" in browser.tab(ctx, {"op": "close", "site": "github"})
    assert calls == [(1, "ctrl+w")]


@pytest.mark.parametrize("handler,action", [(browser.tab, {"op": "new"}), (browser.nav, {"op": "back"})])
def test_nonbrowser_window_refuses_shortcuts(ctx, monkeypatch, handler, action):
    ctx.apps.browser = lambda window: False
    monkeypatch.setattr(browser, "press_window", lambda *args: pytest.fail("must not inject"))
    with pytest.raises(ExecutionError, match="not a verified browser"):
        handler(ctx, action)


def test_browser_shortcut_keeps_verified_target_across_foreground_race(ctx):
    original = ctx.guard

    def guard(window):
        original(window)
        ctx.native.active = 99

    ctx.guard = guard
    ctx.input_backend = SimpleNamespace(modifiers_held=lambda: False,
                                        send=lambda events: pytest.fail("must not inject"))
    with pytest.raises(ExecutionError, match="Foreground changed"):
        browser.nav(ctx, {"op": "back"})
