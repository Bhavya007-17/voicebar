"""Capture tests render owned pixels only; never read a real desktop window."""

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage, QPixmap

from voicebar.screen_frames import ScreenFrames
from voicebar.ui.screen_capture import ScreenCapture


class Native:
    def __init__(self):
        self.hwnd, self.pid = 101, 201
        self.closed = False

    def foreground(self):
        return self.hwnd

    def _pid_tid(self, hwnd):
        return self.pid, 301

    def is_window(self, hwnd, pid):
        return (hwnd, pid) == (self.hwnd, self.pid)

    def shutdown(self):
        self.closed = True


@pytest.fixture
def capture(qapp, monkeypatch):
    now = [10.0]
    state = [True, 3]
    errors, grabbed = [], []
    controller = SimpleNamespace(screen_frames=ScreenFrames(clock=lambda: now[0]),
                                 screen_capture_state=lambda: tuple(state),
                                 screen_capture_error=errors.append,
                                 closed=threading.Event())
    pixmap = QPixmap(2400, 1200)
    pixmap.fill(QColor("gray"))
    on_grab = [lambda: None]

    def grab(hwnd):
        grabbed.append(hwnd)
        on_grab[0]()
        return pixmap

    monkeypatch.setattr(qapp, "primaryScreen", lambda: SimpleNamespace(grabWindow=grab))
    native = Native()
    service = ScreenCapture(controller, qapp, native=native, clock=lambda: now[0])
    service.timer.stop()
    yield SimpleNamespace(service=service, native=native, state=state, now=now, errors=errors,
                          grabbed=grabbed, controller=controller, on_grab=on_grab)
    service.close()
    service.timer.deleteLater()


def test_capture_publishes_bounded_png_for_same_hwnd_pid_generation(capture):
    capture.service.capture()
    frame = capture.controller.screen_frames.get(101, 201, 3)
    assert frame is not None
    assert (frame.hwnd, frame.pid, frame.generation, frame.captured_at) == (101, 201, 3, 10.0)
    image = QImage.fromData(frame.png, "PNG")
    assert (image.width(), image.height()) == (1280, 640)
    assert capture.grabbed == [101]
    assert capture.errors == []
    assert capture.service.timer.interval() == 750


@pytest.mark.parametrize("condition", ["disabled", "zero", "own", "closed"])
def test_capture_never_reads_desktop_or_own_windows_or_disabled_state(capture, condition):
    if condition == "disabled":
        capture.state[0] = False
    elif condition == "zero":
        capture.native.hwnd = 0
    elif condition == "own":
        capture.native.pid = capture.service._pid
    else:
        capture.controller.closed.set()
    capture.service.capture()
    assert capture.grabbed == []
    assert capture.controller.screen_frames.get(101, 201, 3) is None


@pytest.mark.parametrize("condition", ["focus", "pid", "generation", "disabled"])
def test_capture_discards_pixels_if_target_or_authorization_changes(capture, condition):
    capture.service.capture()
    assert capture.controller.screen_frames.get(101, 201, 3) is not None

    def change():
        if condition == "focus":
            capture.native.hwnd = 102
        elif condition == "pid":
            capture.native.pid = 202
        elif condition == "generation":
            capture.state[1] = 4
        else:
            capture.state[0] = False

    capture.on_grab[0] = change
    capture.service.capture()
    assert capture.controller.screen_frames.get(101, 201, 3) is None
    assert capture.controller.screen_frames.get(101, 201, 4) is None


def test_failed_capture_clears_old_frame_and_reports_without_pixels(capture):
    capture.service.capture()

    def fail():
        raise RuntimeError("capture failed")

    capture.on_grab[0] = fail
    capture.service.capture()
    capture.service.capture()
    assert capture.controller.screen_frames.get(101, 201, 3) is None
    assert capture.errors == ["Screen capture unavailable: capture failed"]


def test_null_pixmap_is_not_published(capture, monkeypatch):
    monkeypatch.setattr(capture.service.application, "primaryScreen", lambda: SimpleNamespace(
        grabWindow=lambda _: QPixmap()))
    capture.service.capture()
    assert capture.controller.screen_frames.get(101, 201, 3) is None
    assert len(capture.errors) == 1


def test_small_pixmap_remains_original_size(qapp):
    pixmap = QPixmap(20, 10)
    pixmap.fill(QColor("gray"))
    image = QImage.fromData(ScreenCapture._png(pixmap), "PNG")
    assert (image.width(), image.height()) == (20, 10)


def test_png_encoding_uses_memory_buffer(qapp):
    pixmap = QPixmap(1, 1)
    pixmap.fill(QColor("gray"))
    png = ScreenCapture._png(pixmap)
    buffer = QBuffer()
    buffer.setData(png)
    assert buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    try:
        assert bytes(buffer.readAll()) == png
    finally:
        buffer.close()
