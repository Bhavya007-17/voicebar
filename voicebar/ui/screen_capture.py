"""GUI-thread foreground capture into a single in-memory PNG slot."""

from __future__ import annotations

import os
from time import perf_counter

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer

from ..screen_frames import MAX_FRAME_BYTES, ScreenFrame


class ScreenCapture:
    def __init__(self, controller, application, *, native=None, clock=perf_counter):
        self.controller, self.application = controller, application
        self._native, self._clock = native, clock
        self._pid = os.getpid()
        self._last_error = None
        self.timer = QTimer(application)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.capture)
        application.aboutToQuit.connect(self.close)
        # Injected non-visual controllers need not implement the capture API.
        if callable(getattr(controller, "screen_capture_state", None)):
            self.timer.start()

    def close(self):
        self.timer.stop()
        frames = getattr(self.controller, "screen_frames", None)
        if frames is not None:
            frames.clear()
        if self._native is not None:
            self._native.shutdown()
            self._native = None

    def _report(self, message):
        if self._last_error == message:
            return
        self._last_error = message
        report = getattr(self.controller, "screen_capture_error", None)
        if report is not None:
            report(message)

    @staticmethod
    def _png(pixmap, max_side=1280):
        if pixmap.isNull():
            raise RuntimeError("The foreground window could not be captured")
        if max(pixmap.width(), pixmap.height()) > max_side:
            pixmap = pixmap.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError("The screen image buffer could not be opened")
        try:
            if not pixmap.save(buffer, "PNG"):
                raise RuntimeError("The screen image could not be encoded")
            result = bytes(buffer.data())
        finally:
            buffer.close()
        if len(result) > MAX_FRAME_BYTES:
            if max_side == 960:
                raise RuntimeError("The screen image exceeds the local vision limit")
            return ScreenCapture._png(pixmap, max_side=960)
        return result

    def capture(self):
        closed = getattr(self.controller, "closed", None)
        if closed is not None and closed.is_set():
            self.close()
            return
        frames = self.controller.screen_frames
        try:
            enabled, generation = self.controller.screen_capture_state()
            frames.set_enabled(enabled, generation)
            if not enabled:
                return
            if self._native is None:
                from ..executor.native import NativeWindows
                self._native = NativeWindows()
            native = self._native
            hwnd = native.foreground()
            # Never pass zero to Qt: that would capture the entire desktop.
            if not hwnd:
                frames.clear()
                return
            pid, _ = native._pid_tid(hwnd)
            if (pid == self._pid or not native.is_window(hwnd, pid)
                    or native.foreground() != hwnd):
                frames.clear()
                return
            screen = self.application.primaryScreen()
            if screen is None:
                raise RuntimeError("No screen is available for foreground capture")
            captured_at = self._clock()
            png = self._png(screen.grabWindow(hwnd))
            enabled_now, generation_now = self.controller.screen_capture_state()
            frames.set_enabled(enabled_now, generation_now)
            if (not enabled_now or generation_now != generation
                    or native.foreground() != hwnd or not native.is_window(hwnd, pid)):
                frames.clear()
                return
            if not frames.publish(ScreenFrame(png, hwnd, pid, captured_at, generation)):
                frames.clear()
                return
            report = getattr(self.controller, "screen_capture_error", None)
            if self._last_error is not None and report is not None:
                report(None)
            self._last_error = None
        except (OSError, RuntimeError, ValueError) as error:
            frames.clear()
            self._report(f"Screen capture unavailable: {error}")
