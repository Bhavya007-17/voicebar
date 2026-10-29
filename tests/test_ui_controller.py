"""Runtime lifecycle and revocation tests use only owned in-process fakes."""

import queue
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.stt.base import SAMPLE_RATE, TextEvent
from voicebar.ui.controller import RuntimeController


def event(controller, predicate, timeout=3):
    deadline = time.perf_counter() + timeout
    seen = []
    while True:
        row = controller.events.get(timeout=max(.001, deadline - time.perf_counter()))
        seen.append(row)
        if predicate(row):
            return row
        assert time.perf_counter() < deadline, seen


class Backend:
    def __init__(self):
        self.text = "computer open Notepad"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.closed = False

    def transcribe_segment(self, audio, emit):
        self.entered.set()
        assert self.release.wait(3), "Test did not release fake recognition"
        emit(TextEvent("one", self.text, True, 0, len(audio), time.perf_counter(), "fake"))

    def close(self):
        self.closed = True


class Segmenter:
    last_speech_at = None

    def reset(self):
        self.last_speech_at = None

    def feed(self, audio, at):
        self.last_speech_at = at + len(audio) / SAMPLE_RATE
        return [SimpleNamespace(audio=audio, start=at, end=self.last_speech_at,
                                speech_end=self.last_speech_at)]

    def drain_drops(self):
        return []


class Harness:
    def __init__(self, root, *, autostart=True, **options):
        (root / "config").mkdir()
        (root / "config/settings.yaml").write_text(
            "names: [computer]\nmode: name\nstt: {backend: fake, device: cpu}\n", encoding="utf-8")
        loaded = LoadedCatalog(Catalog(apps=["Notepad", "Calculator"]), [], {}, ["Notepad"])
        self.transitions = queue.Queue()
        self.calls = []
        self.backend = Backend()
        self.unlocked = threading.Event()
        self.unlocked.set()
        harness = self

        class Microphone:
            is_open = False

            def __init__(self, index):
                self.index = index

            @contextmanager
            def open(self, callback):
                self.callback = callback
                self.is_open = True
                harness.transitions.put(("opened", self))
                try:
                    yield {"name": "fake microphone", "index": self.index}
                finally:
                    self.is_open = False
                    harness.transitions.put(("closed", self))

            def speak(self, silent=False):
                pcm = np.full(512, 0 if silent else 1000, dtype="<i2").tobytes()
                self.callback(pcm, 512, "")

        def execute(plan, cancel):
            assert not cancel.is_set()
            self.calls.extend(plan)
            return [{"ok": True, "detail": "Opened owned fake Notepad"}]

        defaults = {"backend_factory": lambda *args, **kwargs: self.backend,
                        "microphone_factory": Microphone, "segmenter_factory": Segmenter,
                        "executor": execute, "available": self.unlocked.is_set,
                        "devices": lambda: [{"index": 5, "name": "Fake mic"}],
                        "startup_timeout": 30, "frame_timeout": 30}
        self.controller = RuntimeController(root, loaded, **{**defaults, **options})
        if autostart:
            self.controller.start()

    def ready(self):
        return event(self.controller, lambda row: row.get("event") == "ready")

    def transition(self, state):
        actual, microphone = self.transitions.get(timeout=3)
        assert actual == state
        return microphone

    def close(self):
        self.backend.release.set()
        self.controller.close()
        assert self.controller.closed.wait(3), "Runtime did not close"


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_starts_off_loads_asynchronously_and_enumerates_microphones(harness):
    row = harness.ready()
    assert not row["enabled"] and not row["stream_open"] and row["state"] == "off"
    assert harness.transitions.empty()
    harness.controller.refresh_microphones()
    row = event(harness.controller, lambda row: row.get("event") == "microphones")
    assert row["devices"] == [{"index": 5, "name": "Fake mic"}]


def test_off_closes_actual_stream_and_emits_confirmed_close(harness):
    harness.ready()
    harness.controller.set_enabled(True)
    microphone = harness.transition("opened")
    harness.controller.set_enabled(False)
    assert harness.transition("closed") is microphone
    row = event(harness.controller, lambda row: row.get("reason") == "stream_closed")
    assert row["stream_open"] is False and row["enabled"] is False
    assert not microphone.is_open
    logs = list((harness.controller.root / "logs").glob("listen-*.jsonl"))
    assert '"reason": "stream_closed"' in logs[0].read_text(encoding="utf-8")


def test_feedback_has_raw_transcript_action_and_latency(harness):
    harness.ready()
    harness.controller.set_enabled(True)
    harness.transition("opened").speak()
    row = event(harness.controller, lambda row: row.get("event") == "decision")
    assert row["text"] == "computer open Notepad" and row["state"] == "done"
    assert row["detail"] == "Opened owned fake Notepad" and row["latency_ms"] >= 0
    assert harness.calls == [{"action": "open_app", "app": "Notepad"}]


def test_unknown_target_shows_two_candidates_and_runs_nothing(harness):
    harness.backend.text = "computer open impossibleunknown"
    harness.ready()
    harness.controller.set_enabled(True)
    harness.transition("opened").speak()
    row = event(harness.controller, lambda row: row.get("event") == "decision")
    assert row["state"] == "not sure" and len(row["candidates"]) == 2
    assert {candidate["name"] for candidate in row["candidates"]} == {"Notepad", "Calculator"}
    assert not harness.calls


def test_off_revokes_recognition_already_in_progress(harness):
    harness.backend.release.clear()
    harness.ready()
    harness.controller.set_enabled(True)
    harness.transition("opened").speak()
    assert harness.backend.entered.wait(3)
    harness.controller.set_enabled(False)
    harness.transition("closed")
    harness.backend.release.set()
    row = event(harness.controller, lambda row: row.get("event") == "decision")
    assert row["reason"] == "stale_or_closed" and not harness.calls


def test_hold_from_off_closes_before_recognition_and_authorizes_one_nameless_command(harness):
    harness.backend.text = "open Notepad"
    harness.ready()
    harness.controller.hold_start()
    microphone = harness.transition("opened")
    microphone.speak()
    harness.controller.hold_end()
    assert harness.transition("closed") is microphone
    row = event(harness.controller, lambda row: row.get("event") == "decision")
    assert row["reason"] == "run" and not row["enabled"] and not row["stream_open"]
    assert harness.calls == [{"action": "open_app", "app": "Notepad"}]
    assert harness.transitions.empty()


def test_silent_hold_does_not_decode_or_run_hallucinated_text(harness):
    harness.ready()
    harness.controller.hold_start()
    harness.transition("opened").speak(silent=True)
    harness.controller.hold_end()
    harness.transition("closed")
    row = event(harness.controller, lambda row: row.get("detail") == "No speech in hold recording")
    assert row["state"] == "not sure" and not harness.backend.entered.is_set() and not harness.calls


def test_hold_arbitrates_with_continuous_stream_and_restores_it(harness):
    harness.ready()
    harness.controller.set_enabled(True)
    continuous = harness.transition("opened")
    harness.controller.hold_start()
    assert harness.transition("closed") is continuous
    held = harness.transition("opened")
    held.speak()
    harness.controller.hold_end()
    assert harness.transition("closed") is held
    event(harness.controller, lambda row: row.get("event") == "decision")
    restored = harness.transition("opened")
    assert restored is not held and not held.is_open


def test_microphone_change_closes_old_stream_before_opening_new(harness):
    harness.ready()
    harness.controller.set_enabled(True)
    original = harness.transition("opened")
    harness.controller.set_microphone(5)
    assert harness.transition("closed") is original
    selected = harness.transition("opened")
    assert selected.index == 5 and not original.is_open


def test_suspend_and_lock_restore_requested_listening_state(harness):
    harness.ready()
    harness.controller.set_enabled(True)
    harness.transition("opened")
    harness.controller.suspend()
    harness.transition("closed")
    harness.controller.resume()
    harness.transition("opened")
    harness.unlocked.clear()
    harness.transition("closed")
    harness.unlocked.set()
    harness.transition("opened")
    harness.controller.set_enabled(False)
    harness.transition("closed")
    harness.controller.suspend()
    harness.controller.resume()
    event(harness.controller, lambda row: row.get("event") == "control" and not row["enabled"])
    assert harness.transitions.empty()


def test_mode_change_revokes_pending_recognition(harness):
    harness.backend.release.clear()
    harness.ready()
    harness.controller.set_enabled(True)
    harness.transition("opened").speak()
    assert harness.backend.entered.wait(3)
    harness.controller.set_mode("open")
    harness.transition("closed")
    harness.transition("opened")
    harness.backend.release.set()
    row = event(harness.controller, lambda row: row.get("event") == "decision")
    assert row["mode"] == "open" and row["reason"] == "stale_or_closed" and not harness.calls


def test_hung_action_times_out_revokes_following_actions_and_keeps_controls_responsive(tmp_path):
    entered, cancelled = threading.Event(), threading.Event()
    effects = []

    def execute(plan, cancel):
        entered.set()
        assert cancel.wait(3), "Controller did not cancel timed out action"
        cancelled.set()
        if not cancel.is_set():
            effects.extend(plan)
        return [{"ok": False, "detail": "Cancelled"}]

    harness = Harness(tmp_path, executor=execute, execution_timeout=.05)
    try:
        harness.backend.text = "computer open Notepad then open Calculator"
        harness.ready()
        harness.controller.set_enabled(True)
        harness.transition("opened").speak()
        assert entered.wait(3)
        row = event(harness.controller, lambda row: row.get("event") == "decision")
        assert row["results"][0]["timed_out"] and row["state"] == "not sure"
        assert cancelled.wait(3)
        harness.transition("closed")
        harness.controller.set_enabled(True)
        row = event(harness.controller, lambda row: row.get("event") == "control" and not row["enabled"])
        assert not effects and not row["enabled"]
    finally:
        harness.close()


def test_model_loading_does_not_block_close_or_controls(tmp_path):
    entered, release = threading.Event(), threading.Event()
    backend = Backend()

    def load(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release model loader"
        return backend

    harness = Harness(tmp_path, backend_factory=load)
    try:
        assert entered.wait(3)
        harness.controller.set_enabled(True)
        harness.controller.set_enabled(False)
        harness.controller.close()
        assert harness.controller.closed.wait(3)
        assert harness.transitions.empty()
    finally:
        release.set()
        harness.close()
