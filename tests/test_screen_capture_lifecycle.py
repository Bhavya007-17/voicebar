"""Pending held speech retains screen authorization without reopening audio."""

import queue
import threading
from types import SimpleNamespace

import pytest

from voicebar.screen_frames import ScreenFrame, ScreenFrames
from voicebar.ui.controller import RuntimeController


@pytest.fixture
def runtime():
    controller = RuntimeController.__new__(RuntimeController)
    controller._vision_settings = {"enabled": True}
    controller._stream_open = False
    controller._hold_pending = True
    controller._capture_generation = 7
    controller.session = SimpleNamespace(generation=7, capture_allowed=True,
                                         gate=SimpleNamespace(asleep=False))
    controller._suspended = controller._fatal = False
    controller._stop = threading.Event()
    controller.screen_frames = ScreenFrames(clock=lambda: 10.0)
    return controller


def publish(runtime):
    return runtime.screen_frames.publish(ScreenFrame(
        b"\x89PNG\r\n\x1a\nowned-pixels", 101, 201, 10.0, runtime.session.generation))


def test_released_hold_keeps_fresh_screen_available_while_audio_is_closed(runtime):
    assert runtime.screen_capture_state() == (True, 7)
    assert publish(runtime)
    assert runtime.screen_frames.get(101, 201, 7) is not None
    assert not runtime._stream_open
    runtime._hold_pending = False
    assert runtime.screen_capture_state() == (False, 7)
    assert runtime.screen_frames.get(101, 201, 7) is None


@pytest.mark.parametrize("revocation", ["generation", "off", "sleep", "suspend", "stop", "fatal", "vision"])
def test_pending_hold_cannot_override_revocation(runtime, revocation):
    assert runtime.screen_capture_state() == (True, 7)
    assert publish(runtime)
    if revocation == "generation":
        runtime.session.generation += 1
    elif revocation == "off":
        runtime.session.capture_allowed = False
    elif revocation == "sleep":
        runtime.session.gate.asleep = True
    elif revocation == "suspend":
        runtime._suspended = True
    elif revocation == "stop":
        runtime._stop.set()
    elif revocation == "fatal":
        runtime._fatal = True
    else:
        runtime._vision_settings["enabled"] = False
    assert runtime.screen_capture_state() == (False, runtime.session.generation)
    assert runtime.screen_frames.get(101, 201, 7) is None
    assert not publish(runtime)


def test_open_microphone_still_captures_without_a_pending_hold(runtime):
    runtime._hold_pending = False
    runtime._stream_open = True
    assert runtime.screen_capture_state() == (True, 7)
    assert publish(runtime)


def test_hold_done_is_queued_only_after_resolution_and_execution_return(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime._segments = queue.Queue()
    runtime._segments.put((7, SimpleNamespace(audio=object(), start=8.0, end=9.0, speech_end=9.0), True))
    runtime._commands = queue.Queue()
    runtime._backend_lock = threading.Lock()
    runtime._backend = object()
    runtime._segmenter = None
    runtime._emit = lambda **kwargs: None
    failures = []
    runtime._fail = failures.append

    def transcribe(backend, audio, emit):
        emit(SimpleNamespace(line_id=1, final=True, text="click the calendar icon", start_sample=0))

    def hear(text, **kwargs):
        assert kwargs["push_to_talk"] and kwargs["generation"] == 7
        entered.set()
        assert release.wait(2)
        runtime._stop.set()

    runtime.session.hear = hear
    monkeypatch.setattr("voicebar.stt.base.transcribe_segment", transcribe)
    worker = threading.Thread(target=runtime._recognize, args=(runtime._backend,))
    worker.start()
    try:
        assert entered.wait(2)
        assert runtime._commands.empty()
        assert runtime.screen_capture_state() == (True, 7)
        assert publish(runtime)
        assert not runtime._stream_open
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []
    assert runtime._commands.get_nowait() == ("hold_done", 7)
