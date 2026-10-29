"""Default-browser launches and browser-guarded keyboard commands."""

from __future__ import annotations

import ctypes as C
import os
import re
import subprocess
from ctypes import wintypes as W
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from ..browser_context import normalized, page_title, tab_selector
from .common import ExecutionError
from .input import _foreground_guard, _send, press_window, unicode_events


class BrowserBackend:
    def launch(self, executable, url, profile=None):
        argv = [executable, "--new-window"]
        if profile is not None:
            argv.append(f"--user-data-dir={Path(profile).resolve()}")
        argv.append(url)
        # The OS-resolved executable is trusted; user URL is one literal argument.
        subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def shell_open(self, url):
        shell = C.WinDLL("shell32", use_last_error=True)
        function = shell.ShellExecuteW
        function.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_int]
        function.restype = C.c_ssize_t
        result = function(None, "open", url, None, None, 1)
        if result <= 32:
            raise ExecutionError(f"Default-browser URL launch failed (ShellExecute {result})")


def _backend(ctx):
    if getattr(ctx, "browser_backend", None) is None:
        ctx.browser_backend = BrowserBackend()
    return ctx.browser_backend


def _same_exe(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _browser(ctx):
    window = ctx.target()
    ctx.guard(window)
    if not ctx.apps.browser(window):
        raise ExecutionError("Foreground target is not a verified browser window")
    return window


def _current_window(ctx, window):
    _foreground_guard(ctx, window)
    current = [item for item in ctx.native.windows()
               if (item.hwnd, item.pid) == (window.hwnd, window.pid)]
    if len(current) != 1:
        raise ExecutionError("The browser window identity changed")
    return current[0]


def _tab_rows(ctx, window):
    from .uia import Automation

    _foreground_guard(ctx, window)
    if getattr(ctx, "automation", None) is None:
        ctx.automation = Automation()
    # Never use an unfiltered tabitem query: webpage ARIA tabs have that same
    # role. This API proves ancestry outside any Document up to this window.
    rows = ctx.automation.browser_tabs(window.hwnd)
    _foreground_guard(ctx, window)
    return [row for row in rows if normalized(row.get("role", "")).replace(" ", "") == "tabitem"]


def _observe(ctx, window):
    session = getattr(ctx, "browser_session", None)
    if session is None:
        return
    # Clear shortcuts stay immediate: a native caption is enough to retain
    # recency; tab identities are resolved only for a later named-site command.
    session.observe_caption(_current_window(ctx, window))


def _open_tab_url(ctx, window, url):
    _observe(ctx, window)
    press_window(ctx, window, "ctrl+t")
    press_window(ctx, window, "ctrl+l")
    for char in url:
        _send(ctx, window, unicode_events(char))
    press_window(ctx, window, "enter")
    ctx.last_app = None
    ctx.save()
    return "URL entered in a new tab of the foreground default browser; page load is not observable"


def open_url(ctx, action):
    url = action["url"]
    scheme = urlsplit(url).scheme
    executable = ctx.native.default_browser(scheme)
    if not executable or not Path(executable).is_absolute():
        raise ExecutionError("Default browser has no verified executable association")
    # Test/demo contexts always create a fresh owned window. Ordinary use may
    # reuse only the already foreground browser with the verified default exe.
    if ctx.owned is None:
        foreground = [window for window in ctx.native.windows()
                      if window.hwnd == ctx.native.foreground()]
        if (len(foreground) == 1 and _same_exe(foreground[0].exe, executable)
                and ctx.apps.browser(foreground[0])):
            return _open_tab_url(ctx, foreground[0], url)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    backend = _backend(ctx)
    if Path(executable).name.casefold() not in {"chrome.exe", "msedge.exe", "chromium.exe"}:
        if ctx.owned is not None:
            raise ExecutionError("Default browser cannot guarantee a new owned window; scoped URL launch refused")
        ctx.begin()
        backend.shell_open(url)
        return "URL handed to the default browser; browser state is not observable"
    ctx.begin()
    backend.launch(executable, url, getattr(ctx, "browser_user_data_dir", None))

    def new_window():
        matches = [window for window in ctx.native.windows()
                   if (window.hwnd, window.pid) not in baseline and _same_exe(window.exe, executable)]
        if len(matches) > 1:
            raise ExecutionError("Default browser created multiple candidate windows; ownership is ambiguous")
        return matches[0] if matches else None

    window = ctx.wait(new_window, "new default-browser window")
    ctx.adopt(window)
    ctx.guard(window)
    ctx.begin()
    ctx.native.activate(window.hwnd, ctx.timeout)
    if ctx.native.foreground() != window.hwnd:
        raise ExecutionError("New default-browser window did not become foreground")
    ctx.last_app = None
    ctx.save()
    return f"New default-browser window confirmed (HWND {window.hwnd}); page load is not observable"


def web_search(ctx, action):
    engine = action["engine"]
    template = ctx.loaded.catalog.engines.get(engine)
    if template is None:
        raise ExecutionError(f"Search engine {engine!r} is not configured")
    return open_url(ctx, {"url": template.replace("{q}", quote_plus(action["query"]))})


def _site_matches(title, hint, hostname):
    if hint and re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", title, re.IGNORECASE):
        return True
    host = hostname.removeprefix("www.") if hostname else ""
    return bool(host and re.search(rf"(?<![\w.-])(?:www\.)?{re.escape(host)}(?![\w.-])",
                                  title, re.IGNORECASE))


def tab(ctx, action):
    window = _browser(ctx)
    op = action["op"]
    if "site" in action and getattr(ctx, "browser_session", None) is not None:
        return _close_site_tab(ctx, window, action["site"])
    _observe(ctx, window)
    if "site" in action:
        site = action["site"]
        url = ctx.loaded.catalog.sites.get(site)
        if not url:
            raise ExecutionError(f"Site {site!r} is not configured")
        hint = ctx.loaded.site_titles.get(site)
        if not _site_matches(window.title, hint, urlsplit(url).hostname):
            raise ExecutionError(f"Active tab title does not match the configured title hint or host for {site!r}")
    keys = {"new": "ctrl+t", "close": "ctrl+w", "next": "ctrl+tab",
            "prev": "ctrl+shift+tab", "reopen": "ctrl+shift+t"}[op]
    before = window.title
    press_window(ctx, window, keys)
    if op == "close":
        def changed():
            if not ctx.native.is_window(window.hwnd, window.pid):
                return True
            matches = [item for item in ctx.native.windows()
                       if item.hwnd == window.hwnd and item.pid == window.pid]
            return bool(matches and matches[0].title != before)
        ctx.wait(changed, "tab close title or window change; identical titles cannot be confirmed")
        return "Tab close confirmed by browser title or window change"
    return "Browser tab shortcut inserted; tab content is not observable"


def _close_site_tab(ctx, window, site):
    from .uia import unique

    url = ctx.loaded.catalog.sites.get(site)
    if not url:
        raise ExecutionError(f"Site {site!r} is not configured")
    session = ctx.browser_session
    rows = _tab_rows(ctx, window)
    current = _current_window(ctx, window)
    chosen = session.choose(current, rows, hint=ctx.loaded.site_titles.get(site),
                            hostname=urlsplit(url).hostname)
    selector = tab_selector(chosen)

    def guard():
        _foreground_guard(ctx, window)
        ctx.begin()

    # Selecting through UIA re-reads the exact selector immediately before its
    # mutation. No title substring, ordinal tab index, or guessed key count.
    ctx.automation.click(window.hwnd, selector, guard)

    def selected():
        latest = _current_window(ctx, window)
        return normalized(page_title(latest.title)) == normalized(chosen["name"])

    ctx.wait(selected, "selected browser tab title")
    current_rows = _tab_rows(ctx, window)
    unique(current_rows, selector)
    if sum(normalized(row.get("name", "")) == normalized(chosen["name"])
           for row in current_rows) != 1:
        raise ExecutionError("Selected browser tab became ambiguous before close; no close key was sent")
    if not selected():
        raise ExecutionError("Selected browser tab changed before close; no close key was sent")
    press_window(ctx, window, "ctrl+w")
    remaining = []

    def closed():
        if not ctx.native.is_window(window.hwnd, window.pid):
            return True
        rows_now = _tab_rows(ctx, window)
        remaining[:] = rows_now
        return not any(all(normalized(row.get(key, "")) == normalized(value)
                           for key, value in selector.items()) for row in rows_now)

    ctx.wait(closed, "selected tab to disappear from browser accessibility")
    if ctx.native.is_window(window.hwnd, window.pid):
        session.observe(_current_window(ctx, window), remaining)
    else:
        session.forget(window)
    return f"Closed {chosen['name']!r}; disappearance confirmed in the current browser window"


def nav(ctx, action):
    window = _browser(ctx)
    _observe(ctx, window)
    keys = {"back": "alt+left", "forward": "alt+right", "reload": "ctrl+r"}[action["op"]]
    press_window(ctx, window, keys)
    return "Browser navigation shortcut inserted; page state is not observable"
