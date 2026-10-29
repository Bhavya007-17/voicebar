"""Real desktop tests: every mutated HWND was opened by this fixture.

Only a second-monitor check is skipped on this laptop. Desktop tests are part of
the default Windows suite, not an opt-in gate or mocked success.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar.executor import Context, execute

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows desktop required")


def run(ctx, *actions):
    results = execute(list(actions), context=ctx)
    assert all(result["ok"] for result in results), results
    return results


@pytest.fixture(scope="module")
def desktop(tmp_path_factory):
    from voicebar.executor.input import InputBackend
    ctx = Context(Path(__file__).resolve().parents[1], owned={}, persist=False)
    # Keep the ownership journal with this pytest run's retained artifacts.
    # The production atomic writer and HWND/PID ownership checks stay intact.
    ctx.ownership_record = tmp_path_factory.mktemp("desktop-ownership") / "owned.json"
    ctx.input_backend = InputBackend()
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    blank_verified = False
    try:
        run(ctx, {"action": "open_app", "app": "Notepad"})
        window = ctx.target("Notepad")
        assert (window.hwnd, window.pid) not in baseline
        def editor_ready():
            try:
                return (True, ctx.native.read_text(window.hwnd))
            except RuntimeError as error:
                if "no readable standard edit control" in str(error):
                    return None
                raise
        _, original = ctx.wait(editor_ready, "owned Notepad's readable editor")
        if original:
            # Modern Notepad may restore documents even in our newly opened
            # window. Create a fresh document; never clear the restored text.
            run(ctx, {"action": "press", "keys": "ctrl+n"})
            _, original = ctx.wait(
                lambda: (True, "") if ctx.native.read_text(window.hwnd) == "" else None,
                "new blank document in owned Notepad")
        assert original == "", "Notepad restored text; leaving it untouched"
        blank_verified = True
        yield ctx
    finally:
        # Clear only our Notepad, so WM_CLOSE cannot leave text or a save prompt.
        failures = []
        for window in ctx.native.windows():
            if ctx.owned.get(window.hwnd) != window.pid:
                continue
            try:
                ctx.guard(window)
                if ctx.apps.matches(window, "Notepad"):
                    if not blank_verified:
                        failures.append("Notepad content was not proven blank; left untouched")
                        continue
                    ctx.native.activate(window.hwnd, ctx.timeout)
                    ctx.native.clear_text(window.hwnd)
                ctx.native.close(window.hwnd)
                ctx.wait(lambda target=window: not ctx.native.is_window(target.hwnd, target.pid),
                         "owned test window to close")
            except (RuntimeError, OSError) as error:
                failures.append(f"{window.hwnd}/{window.pid}: {error}")
        ctx.close()
        assert not failures, "Owned-window cleanup failed: " + "; ".join(failures)


def test_notepad_identity_foreground_and_placement(desktop):
    ctx = desktop
    window = ctx.target("Notepad")
    assert ctx.apps.matches(window, "Notepad")
    assert window.title
    assert ctx.native.foreground() == window.hwnd
    for op, show in (("maximize", 3), ("minimize", 2), ("restore", 1)):
        run(ctx, {"action": "window", "op": op, "target": "Notepad"})
        assert ctx.native.placement(window.hwnd)["show"] == show
    run(ctx, {"action": "switch_app", "app": "Notepad"})
    assert ctx.native.foreground() == window.hwnd


def test_short_unicode_and_submit_are_read_back(desktop):
    ctx = desktop
    window = ctx.target("Notepad")
    ctx.native.activate(window.hwnd, ctx.timeout)
    ctx.native.clear_text(window.hwnd)
    text = "Hello, World! 123 café Ελληνικά 中文 🚀"
    run(ctx, {"action": "type_text", "text": text, "submit": True})
    observed = ctx.wait(lambda: (value if text in (value := ctx.native.read_text(window.hwnd))
                                and value.endswith(("\r\n", "\n")) else None),
                        "typed Unicode and Enter in owned Notepad")
    assert observed.replace("\r\n", "\n") == text + "\n"
    ctx.native.clear_text(window.hwnd)


def test_long_text_paste_restores_clipboard(desktop):
    ctx = desktop
    window = ctx.target("Notepad")
    ctx.native.activate(window.hwnd, ctx.timeout)
    ctx.native.clear_text(window.hwnd)
    before = clipboard_fingerprint(ctx.input_backend)
    text = "Capital letters, punctuation: café 中文 🚀. " * 12
    run(ctx, {"action": "type_text", "text": text, "submit": False})
    assert ctx.wait(lambda: ctx.native.read_text(window.hwnd) == text,
                    "long clipboard-pasted text")
    assert clipboard_fingerprint(ctx.input_backend) == before
    ctx.native.clear_text(window.hwnd)


def clipboard_fingerprint(backend):
    """Compare format IDs and HGLOBAL digests without logging clipboard contents."""
    import ctypes as C
    import hashlib
    from ctypes import wintypes as W

    backend.kernel.GlobalSize.argtypes = [W.HANDLE]
    backend.kernel.GlobalSize.restype = C.c_size_t
    assert backend.user.OpenClipboard(None), "Clipboard busy during read-only fingerprint"
    try:
        result, fmt = {}, 0
        while (fmt := int(backend.user.EnumClipboardFormats(fmt))):
            handle = backend.user.GetClipboardData(fmt)
            assert handle, f"Cannot read clipboard format {fmt}"
            if fmt in {2, 3, 9, 14, 0x82, 0x83, 0x8E}:
                result[fmt] = "GDI format present; deep copying covered by unit tests"
                continue
            size = backend.kernel.GlobalSize(handle)
            pointer = backend.kernel.GlobalLock(handle)
            assert pointer or size == 0, f"Cannot lock clipboard format {fmt}"
            try:
                result[fmt] = hashlib.sha256(C.string_at(pointer, size) if size else b"").hexdigest()
            finally:
                if pointer:
                    backend.kernel.GlobalUnlock(handle)
        return result
    finally:
        backend.user.CloseClipboard()


def test_multiline_append_and_unicode_selection_replacement(desktop):
    ctx = desktop
    window = ctx.target("Notepad")
    ctx.native.activate(window.hwnd, ctx.timeout)
    ctx.native.clear_text(window.hwnd)
    run(ctx, {"action": "type_text", "text": "First café", "submit": True})
    ctx.wait(lambda: ctx.native.read_text(window.hwnd).replace("\r\n", "\n") == "First café\n",
             "first line and Enter")
    run(ctx, {"action": "type_text", "text": "第二行 🚀", "submit": False})
    assert ctx.native.read_text(window.hwnd).replace("\r\n", "\n") == "First café\n第二行 🚀"
    run(ctx, {"action": "press", "keys": "ctrl+a"})
    run(ctx, {"action": "type_text", "text": "Replaced 😀", "submit": False})
    assert ctx.native.read_text(window.hwnd) == "Replaced 😀"
    ctx.native.clear_text(window.hwnd)


def test_snap_queries_physical_bounds(desktop):
    ctx = desktop
    window = ctx.target("Notepad")
    for op in ("snap_left", "snap_right"):
        run(ctx, {"action": "window", "op": op, "target": "Notepad"})
        monitor = next(item for item in ctx.native.monitors()
                       if item.handle == ctx.native.monitor(window.hwnd))
        left, top, right, bottom = monitor.work
        rect = ctx.native.rect(window.hwnd)
        mid = left + (right - left) // 2
        expected = (left, top, mid, bottom) if op == "snap_left" else (mid, top, right, bottom)
        assert all(abs(actual - wanted) <= 16 for actual, wanted in zip(rect, expected)), (rect, expected)


def test_move_maximized_to_other_monitor_and_back(desktop):
    ctx = desktop
    if len(ctx.native.monitors()) < 2:
        pytest.skip("Only one monitor; crossing monitors cannot be tested")
    window = ctx.target("Notepad")
    original = ctx.native.monitor(window.hwnd)
    run(ctx, {"action": "window", "op": "maximize", "target": "Notepad"},
        {"action": "window", "op": "move_monitor", "target": "Notepad", "monitor": "next"})
    assert ctx.native.monitor(window.hwnd) != original
    assert ctx.native.placement(window.hwnd)["show"] == 3
    run(ctx, {"action": "window", "op": "move_monitor", "target": "Notepad", "monitor": "prev"})
    assert ctx.native.monitor(window.hwnd) == original
    assert ctx.native.placement(window.hwnd)["show"] == 3


def test_calculator_mapping_switch_close_and_browser_refusal(desktop):
    ctx = desktop
    run(ctx, {"action": "open_app", "app": "Calculator"},
        {"action": "window", "op": "maximize", "target": "Calculator"})
    window = ctx.target("Calculator")
    assert ctx.apps.matches(window, "Calculator")
    assert ctx.native.foreground() == window.hwnd
    assert ctx.native.placement(window.hwnd)["show"] == 3
    result = execute([{"action": "nav", "op": "reload"}], context=ctx)
    assert not result[0]["ok"] and "browser" in result[0]["detail"].casefold()
    run(ctx, {"action": "close_app", "app": "Calculator"})
    assert ctx.wait(lambda: not ctx.native.is_window(window.hwnd, window.pid), "Calculator closed")


@pytest.mark.parametrize("action", [
    {"action": "tab", "op": "new"},
    {"action": "tab", "op": "close"},
    {"action": "tab", "op": "close", "site": "youtube"},
    {"action": "tab", "op": "next"},
    {"action": "tab", "op": "prev"},
    {"action": "tab", "op": "reopen"},
    {"action": "nav", "op": "back"},
    {"action": "nav", "op": "forward"},
    {"action": "nav", "op": "reload"},
])
def test_owned_notepad_refuses_browser_actions_without_mutation(desktop, monkeypatch, action):
    ctx = desktop
    window = ctx.target("Notepad")
    ctx.native.activate(window.hwnd, ctx.timeout)
    assert ctx.native.foreground() == window.hwnd
    assert ctx.owned[window.hwnd] == window.pid
    assert not ctx.apps.browser(window)
    text = ctx.native.read_text(window.hwnd)
    title = ctx.target("Notepad").title
    monkeypatch.setattr(ctx.input_backend, "send",
                        lambda events: pytest.fail("Browser refusal must happen before input"))

    result = execute([action], context=ctx)

    assert not result[0]["ok"]
    assert "not a verified browser" in result[0]["detail"]
    assert result[0]["started_ms"] is None
    assert ctx.native.is_window(window.hwnd, window.pid)
    assert ctx.native.read_text(window.hwnd) == text
    assert ctx.target("Notepad").title == title


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_installed_default_browser_identity_is_read_only(desktop, scheme):
    """Check installed association metadata without opening or driving a browser."""
    ctx = desktop
    executable = ctx.native.default_browser(scheme)
    assert Path(executable).is_absolute()
    assert Path(executable).is_file()
    assert ctx.apps.browser(SimpleNamespace(exe=executable, aumids=()))
