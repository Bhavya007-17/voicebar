"""A real mouse click on the bar must leave an owned Notepad foreground.

This test is part of the normal Windows gate. It never activates, types into,
clicks, or closes a pre-existing app window. The cursor is restored afterwards.
"""

import ctypes as C
import os
import sys
from ctypes import wintypes as W
from pathlib import Path
from time import monotonic

import pytest

from voicebar.executor import Context, execute

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows desktop required")


@pytest.fixture
def owned_notepad(qapp):
    """Open an owned editor; Windows may restore its previous session.

    This focus-only test sends no keyboard input and never clears text or
    changes documents. A restored session is safe to observe and close.
    """
    ctx = Context(Path(__file__).resolve().parents[1], owned={}, persist=False)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    window = None
    try:
        results = execute([{"action": "open_app", "app": "Notepad"}], context=ctx)
        assert results and all(row["ok"] for row in results), results
        window = ctx.target("Notepad")
        assert (window.hwnd, window.pid) not in baseline

        yield ctx, window
    finally:
        try:
            if window is not None and ctx.native.is_window(window.hwnd, window.pid):
                ctx.guard(window)
                # Close only this test's HWND; do not clear any restored document.
                ctx.native.close(window.hwnd)
                ctx.wait(lambda: not ctx.native.is_window(window.hwnd, window.pid),
                         "owned focus-test Notepad to close")
        finally:
            ctx.close()


def test_bar_show_expand_and_native_click_never_take_focus(qapp, owned_notepad):
    from PySide6.QtCore import QAbstractAnimation, QEvent, QObject

    from voicebar.executor.input import INPUT, InputBackend
    from voicebar.ui.bar import TopBar

    app = qapp
    assert app.platformName() == "windows", "The no-focus gate needs the real Windows Qt backend"
    ctx, notepad = owned_notepad
    native = ctx.native
    backend = InputBackend()
    user = backend.user
    user.SetCursorPos.argtypes, user.SetCursorPos.restype = [C.c_int, C.c_int], W.BOOL
    user.GetWindowLongPtrW.argtypes = [W.HWND, C.c_int]
    user.GetWindowLongPtrW.restype = C.c_ssize_t
    old_cursor = W.POINT()
    assert user.GetCursorPos(C.byref(old_cursor)), "Cannot preserve the cursor position"
    # Never release a button held by the person using the computer.
    assert not any(backend.async_key(vk) & 0x8000 for vk in (1, 2, 4, 5, 6)), "Mouse button held"
    bar = TopBar({"monitor": "primary", "dwell_ms": 450, "idle_ms": 4000})

    class ClickWitness(QObject):
        released = False

        def eventFilter(self, receiver, event):
            if event.type() == QEvent.Type.MouseButtonRelease:
                self.released = True
            return False

    witness = ClickWitness(bar)
    bar.installEventFilter(witness)
    bar.listen_button.installEventFilter(witness)
    toggles = []
    bar.listening_changed.connect(toggles.append)

    def still_notepad():
        assert native.is_window(notepad.hwnd, notepad.pid), "Owned Notepad disappeared"
        assert native.foreground() == notepad.hwnd, "The bar took foreground focus from Notepad"

    def pump_until(predicate, description):
        deadline = monotonic() + 5
        while True:
            app.processEvents()
            still_notepad()
            if predicate():
                return
            assert monotonic() < deadline, f"Timed out waiting for {description}"

    try:
        native.activate(notepad.hwnd, ctx.timeout)
        still_notepad()
        bar.show()
        still_notepad()
        pump_until(lambda: bar.isVisible(), "bar to show without activation")
        hwnd = int(bar.winId())
        assert native.is_window(hwnd, os.getpid()), "Bar HWND does not belong to this test process"
        styles = int(user.GetWindowLongPtrW(hwnd, -20))
        assert styles & 0x08000000, "Bar must have WS_EX_NOACTIVATE"
        assert styles & 0x00000080, "WS_EX_TOOLWINDOW excludes the bar from Alt+Tab and the taskbar"
        assert styles & 0x00000008, "Bar must be WS_EX_TOPMOST"
        assert not styles & 0x00040000, "WS_EX_APPWINDOW would force a taskbar entry"

        bar.expand()
        still_notepad()
        pump_until(lambda: bar.is_expanded and bar.animation.state() == QAbstractAnimation.State.Stopped
                   and bar.height() == bar.expanded_height, "bar expansion to finish")

        # GetWindowRect is read under the existing per-monitor DPI context. Its
        # physical coordinates match SetCursorPos even with mixed display scale.
        left, top, right, bottom = native.rect(hwnd)
        point = W.POINT((left + right) // 2,
                        top + min(round(10 * bar.devicePixelRatioF()), (bottom - top) // 3))
        assert user.SetCursorPos(point.x, point.y), "Cannot move the cursor onto the owned bar"
        under_pointer = int(user.WindowFromPoint(point) or 0)
        assert int(user.GetAncestor(under_pointer, 2) or 0) == hwnd, "Click target is not the owned bar"
        still_notepad()
        # Use real input, not QTest's synthetic widget dispatch: Windows must
        # exercise WM_MOUSEACTIVATE and the actual non-activating window styles.
        events = backend._array([(0, 0, 0, 0x0002), (0, 0, 0, 0x0004)])
        inserted = int(backend.send_input(2, events, C.sizeof(INPUT)))
        if inserted == 1:
            release = backend._array([(0, 0, 0, 0x0004)])
            assert backend.send_input(1, release, C.sizeof(INPUT)) == 1, "Cannot release injected click"
        assert inserted == 2, f"SendInput inserted {inserted}/2 mouse events"
        pump_until(lambda: witness.released, "the native bar click to be delivered")
        still_notepad()
        # Exercise an interactive control too; the signal has no audio handler
        # in this isolated bar. Clicking must work without activating Qt.
        centre = bar.listen_button.geometry().center()
        scale = bar.devicePixelRatioF()
        point = W.POINT(left + round(centre.x() * scale), top + round(centre.y() * scale))
        assert user.SetCursorPos(point.x, point.y)
        under_pointer = int(user.WindowFromPoint(point) or 0)
        assert int(user.GetAncestor(under_pointer, 2) or 0) == hwnd
        assert backend.send_input(2, events, C.sizeof(INPUT)) == 2
        pump_until(lambda: toggles == [True], "the listening button's native click")
        still_notepad()
        bar.collapse()
        pump_until(lambda: not bar.is_expanded
                   and bar.animation.state() == QAbstractAnimation.State.Stopped,
                   "bar collapse without activation")
    finally:
        bar.close()
        bar.deleteLater()
        app.processEvents()
        assert user.SetCursorPos(old_cursor.x, old_cursor.y), "Cannot restore the cursor position"
        backend.close()
