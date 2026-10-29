"""All hotkeys, microphone streams, recognizers and executors here are fake."""

import json
import struct
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from voicebar import ptt
from voicebar.catalog import Catalog, InstalledApp, LoadedCatalog
from voicebar.ptt import (
    PttCleanupError,
    PttError,
    PttExecution,
    WindowsHotkey,
    listen_ptt,
    parse_hotkey,
)
from voicebar.schema import SchemaError
from voicebar.stt.base import TextEvent

PCM = struct.pack("<h", 1500) * 512


@pytest.fixture(autouse=True)
def isolated_native_capture_boundary(monkeypatch):
    # A real cleanup failure is permanent until process exit. Each fake-only
    # test gets its own process-state flag after unblocking its worker.
    monkeypatch.setattr(ptt, "_CAPTURE_UNSAFE", threading.Event())


@pytest.fixture
def loaded():
    catalog = Catalog(apps=["Notepad", "Calculator"], engines={"google": "https://google.com/search?q={q}"})
    return LoadedCatalog(catalog, [InstalledApp(name, name) for name in catalog.apps], {}, [])


class FakeHotkeys:
    def __init__(self, holds=1):
        self.remaining, self.is_held = holds, True
        self.registered, self.closed, self.unlocked = False, False, True

    def register(self):
        self.registered = True

    def wait_pressed(self, stop):
        if self.remaining == 0:
            return False
        self.remaining -= 1
        self.is_held = True
        return True

    def held(self):
        return self.is_held

    def available(self):
        return self.unlocked

    def close(self):
        self.closed = True
        self.registered = False


class FakeMicrophone:
    def __init__(self, controls, *, chunks=12, status="", release=True, pcm=PCM, on_close=None):
        self.controls, self.chunks, self.status = controls, chunks, status
        self.release, self.pcm, self.on_close = release, pcm, on_close
        self.opened, self.closed = 0, 0
        self.callback_thread = None
        self.is_open = False

    @contextmanager
    def open(self, callback):
        self.opened += 1
        self.is_open = True
        try:
            self.callback_thread = threading.get_ident()
            for _ in range(self.chunks):
                callback(self.pcm, 512, self.status)
            if self.release:
                self.controls.is_held = False
            yield {"name": "Fake default mic", "selection": "system-default-input"}
        finally:
            self.closed += 1
            self.is_open = False
            if self.on_close:
                self.on_close()


class FakeBackend:
    name, device = "fake", "cpu"

    def __init__(self, texts=None):
        self.texts = iter(texts or ["open Notepad"])
        self.closed = 0
        self.feed_threads = []

    def start(self, emit):
        self.emit, self.frames = emit, 0

    def feed(self, audio):
        self.feed_threads.append(threading.get_ident())
        self.frames += len(audio)
        self.emit(TextEvent("line", "wrong provisional command", False, 0, self.frames,
                            time.perf_counter(), self.name))

    def finish(self):
        self.emit(TextEvent("line", next(self.texts), True, 0, self.frames,
                            time.perf_counter(), self.name))

    def close(self):
        self.closed += 1


class DelayedNativeMic:
    """Event-controlled fake for native open/start/close; never touches audio."""

    def __init__(self, controls, *, open_allowed=True, close_allowed=True,
                 frames=True, release=True, late_close_frame=False):
        self.controls, self.frames, self.release = controls, frames, release
        self.late_close_frame = late_close_frame
        self.open_entered, self.close_entered, self.finished = (threading.Event() for _ in range(3))
        self.allow_open, self.allow_close = threading.Event(), threading.Event()
        if open_allowed:
            self.allow_open.set()
        if close_allowed:
            self.allow_close.set()
        self.opened, self.closed, self.is_open = 0, 0, False

    @contextmanager
    def open(self, callback):
        self.opened += 1
        self.is_open = True
        self.open_entered.set()
        try:
            self.allow_open.wait(2)  # safety escape if the test itself fails
            if self.frames:
                for _ in range(12):
                    callback(PCM, 512, "")
            if self.release:
                self.controls.is_held = False
            yield {"name": "Fake delayed native device"}
        finally:
            self.close_entered.set()
            if self.late_close_frame:
                callback(PCM, 512, "")
            self.allow_close.wait(2)
            self.closed += 1
            self.is_open = False
            self.finished.set()

    def unblock(self):
        self.allow_open.set()
        self.allow_close.set()
        assert self.finished.wait(1)


def run(root, loaded, *, controls=None, mic=None, backend=None, executor=None, **kwargs):
    controls = controls or FakeHotkeys()
    mic = mic or FakeMicrophone(controls)
    backend = backend or FakeBackend()
    calls = []
    if executor is None:
        def executor(plan):
            calls.append(plan)
            return PttExecution([{"ok": True}], time.perf_counter())
    output = []
    state_reader = kwargs.pop("state_reader", lambda: None)
    result = listen_ptt(root, loaded, hotkeys=controls, microphone=mic,
                        backend_factory=lambda *args, **kw: backend,
                        executor=executor, state_reader=state_reader,
                        output_fn=output.append, **kwargs)
    return SimpleNamespace(report=result, controls=controls, mic=mic, backend=backend,
                           calls=calls, output=output)


def test_hotkey_canonical_modifiers_and_no_repeat_registration():
    hotkey = parse_hotkey(" Alt + Control + F8 ")
    assert hotkey.text == "ctrl+alt+f8"
    assert hotkey.key == 0x77
    captured = []
    native = object.__new__(WindowsHotkey)
    native.hotkey, native.registered, native.identifier = hotkey, False, 123
    native.user32 = SimpleNamespace(RegisterHotKey=lambda *args: captured.append(args) or 1,
                                    UnregisterHotKey=lambda *args: 1)
    native.register()
    assert captured == [(None, 123, 3 | 0x4000, 0x77)]
    native.close()
    assert not native.registered


@pytest.mark.parametrize("text", ["f8", "ctrl+ctrl+f8", "win+h", "ctrl+f12", "alt+space", "ctrl+foo", "ctrl++f8"])
def test_reserved_or_malformed_hotkeys_are_rejected(text):
    with pytest.raises(PttError):
        parse_hotkey(text)


def test_held_uses_high_bit_only_not_pressed_since_last_query():
    native = object.__new__(WindowsHotkey)
    native.hotkey = parse_hotkey("ctrl+alt+f8")
    native.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 1)
    assert not native.held()
    native.user32.GetAsyncKeyState = lambda key: -32768
    assert native.held()


@pytest.mark.parametrize("desktop,expected", [("Default", True), ("Winlogon", False), ("other", False)])
def test_secure_desktop_check_closes_its_read_only_handle(desktop, expected):
    closed = []
    def information(handle, index, buffer, size, needed):
        buffer.value = desktop
        return 1
    native = object.__new__(WindowsHotkey)
    native.user32 = SimpleNamespace(OpenInputDesktop=lambda *args: 77,
                                    GetUserObjectInformationW=information,
                                    CloseDesktop=lambda handle: closed.append(handle))
    assert native.available() is expected
    assert closed == [77]


def test_collision_fails_before_model_loading_or_audio(tmp_path, loaded):
    class Collision(FakeHotkeys):
        def register(self):
            raise PttError("hotkey collision")
    controls = Collision()
    mic = FakeMicrophone(controls)
    def factory(*args, **kwargs):
        pytest.fail("collision must fail before model loading")
    with pytest.raises(PttError, match="collision"):
        listen_ptt(tmp_path, loaded, hotkeys=controls, microphone=mic, backend_factory=factory)
    assert controls.closed and mic.opened == 0
    failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
    assert failure["status"] == "error" and failure["capture_closed"]
    assert failure["executed"] is False and not failure["execution_attempted"]


def test_waiting_keeps_microphone_closed_and_shutdown_releases_resources(tmp_path, loaded):
    controls = FakeHotkeys(holds=0)
    session = run(tmp_path, loaded, controls=controls)
    assert session.report.utterances == 0
    assert session.mic.opened == 0
    assert session.backend.closed == 1
    assert controls.closed


def test_release_closes_mic_then_final_executes_once_on_worker_transcript(tmp_path, loaded):
    session = run(tmp_path, loaded)
    result = session.report.last_result
    assert session.mic.opened == session.mic.closed == 1
    assert session.backend.closed == 1 and session.controls.closed
    assert session.calls == [[{"action": "open_app", "app": "Notepad"}]]
    assert result["text"] == "open Notepad" and result["executed"]
    assert result["release_to_final_ms"] is not None
    assert result["release_to_action_started_ms"] >= result["release_to_final_ms"]
    assert all(ident != session.mic.callback_thread for ident in session.backend.feed_threads)
    log = [json.loads(line) for line in session.report.log_path.read_text().splitlines()]
    assert len(log) == 1 and log[0]["plan"] == session.calls[0]


def test_release_clock_includes_stream_closure_and_recognition_backlog(tmp_path, loaded):
    controls = FakeHotkeys()
    mic = FakeMicrophone(controls, on_close=lambda: threading.Event().wait(0.025))
    session = run(tmp_path, loaded, controls=controls, mic=mic)
    assert session.report.last_result["release_to_microphone_closed_ms"] >= 20
    assert session.report.last_result["release_to_final_ms"] >= 20


def test_ready_cue_waits_for_first_received_frame_and_is_emitted_once(tmp_path, loaded):
    controls, backend = FakeHotkeys(), FakeBackend()
    received = threading.Event()
    before_frame, output = [], []
    class DelayedMic(FakeMicrophone):
        @contextmanager
        def open(self, callback):
            with super().open(lambda *args: None) as device:
                def send():
                    before_frame.extend(output)
                    callback(PCM, 512, "")
                    received.set()
                timer = threading.Timer(0.025, send)
                timer.start()
                try:
                    yield device
                finally:
                    timer.cancel()
                    timer.join()
    mic = DelayedMic(controls, chunks=0, release=False)
    def display(text):
        if text == "Ready — speak":
            assert received.wait(1) and mic.is_open
            controls.is_held = False
        output.append(text)
    report = listen_ptt(tmp_path, loaded, hotkeys=controls, microphone=mic,
                        backend_factory=lambda *args, **kwargs: backend,
                        dry_run=True, state_reader=lambda: None, output_fn=display)
    assert "Ready — speak" not in before_frame
    assert output.count("Ready — speak") == 1
    assert report.last_result["ready_cue_emitted"] and report.last_result["capture_closed"]


def test_terminal_ready_cue_flushes_immediately(monkeypatch):
    calls = []
    def terminal(text, *, flush=False):
        calls.append((text, flush))
    monkeypatch.setattr(ptt, "print", terminal, raising=False)
    ptt._say(terminal, "Ready — speak")
    assert calls == [("Ready — speak", True)]


def test_dry_run_never_calls_executor(tmp_path, loaded):
    def executor(plan):
        pytest.fail("dry-run must never execute")
    session = run(tmp_path, loaded, dry_run=True, executor=executor)
    assert session.report.last_result["plan"] == [{"action": "open_app", "app": "Notepad"}]
    assert not session.report.last_result["executed"]


def test_unknown_and_silent_audio_cannot_execute(tmp_path, loaded):
    unknown = run(tmp_path, loaded, backend=FakeBackend(["open the imaginary unicorn"]))
    assert unknown.report.last_result["text"] == "open the imaginary unicorn"
    assert unknown.report.last_result["plan"] is None and unknown.calls == []
    controls = FakeHotkeys()
    silence = run(tmp_path, loaded, controls=controls,
                  mic=FakeMicrophone(controls, pcm=b"\0\0" * 512))
    assert silence.report.last_result["text"] == "" and silence.calls == []


def test_malformed_plan_fails_complete_preflight_before_execution(tmp_path, loaded, monkeypatch):
    monkeypatch.setattr(ptt, "parse", lambda *args, **kwargs: [
        {"action": "open_app", "app": "Notepad"}, {"action": "shell", "command": "bad"}
    ])
    calls = []
    controls = FakeHotkeys()
    mic, backend = FakeMicrophone(controls), FakeBackend()
    with pytest.raises(SchemaError):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend, executor=calls.append)
    assert calls == [] and mic.closed == 1 and controls.closed and backend.closed == 1
    failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
    assert failure["error_type"] == "SchemaError" and failure["capture_closed"]
    assert not failure["executed"] and not failure["execution_attempted"]


@pytest.mark.parametrize("status,pcm,reason", [
    ("input overflow", PCM, "lost"), ("", b"\0", "malformed"),
])
def test_capture_loss_or_malformed_frames_discard_command(tmp_path, loaded, status, pcm, reason):
    controls = FakeHotkeys()
    mic, backend = FakeMicrophone(controls, status=status, pcm=pcm), FakeBackend()
    calls = []
    with pytest.raises(PttError, match=reason):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend, executor=calls.append)
    assert calls == [] and mic.closed == 1 and controls.closed and backend.closed == 1


def test_audio_backlog_is_bounded_and_overflow_never_executes(tmp_path, loaded):
    unblock = threading.Event()
    class SlowBackend(FakeBackend):
        def feed(self, audio):
            assert unblock.wait(1)
            super().feed(audio)
    controls = FakeHotkeys()
    mic = FakeMicrophone(controls, chunks=50, on_close=unblock.set)
    backend, calls = SlowBackend(), []
    with pytest.raises(PttError, match="backlog"):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend,
            executor=calls.append, backlog_seconds=0.032)
    assert calls == [] and mic.closed == 1 and backend.closed == 1 and controls.closed


def test_recognition_failure_closes_audio_and_never_executes(tmp_path, loaded):
    class FailedBackend(FakeBackend):
        def finish(self):
            raise RuntimeError("model decode failed")
    controls = FakeHotkeys()
    mic, backend, calls = FakeMicrophone(controls), FailedBackend(), []
    with pytest.raises(PttError, match="model decode failed"):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend, executor=calls.append)
    assert calls == [] and mic.closed == 1 and controls.closed and backend.closed == 1
    failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
    assert failure["error"] == "model decode failed" and failure["capture_closed"]
    assert failure["captured_frames"] == 12 * 512
    assert failure["executed"] is False and not failure["execution_attempted"]


def test_hold_cap_discards_partial_commands_and_closes_mic(tmp_path, loaded):
    controls = FakeHotkeys()
    mic, calls = FakeMicrophone(controls, chunks=1, release=False), []
    with pytest.raises(PttError, match="recording cap"):
        run(tmp_path, loaded, controls=controls, mic=mic, executor=calls.append, max_seconds=0.02)
    assert mic.closed == 1 and controls.closed and calls == []


def test_timeout_closes_mic_and_hotkey_without_closing_inflight_model(tmp_path, loaded):
    unblock, model_closed = threading.Event(), threading.Event()
    class HungBackend(FakeBackend):
        def feed(self, audio):
            unblock.wait(2)
        def close(self):
            super().close()
            model_closed.set()
    controls = FakeHotkeys()
    mic, backend, calls = FakeMicrophone(controls, chunks=1), HungBackend(), []
    try:
        with pytest.raises(PttError, match="deadline"):
            run(tmp_path, loaded, controls=controls, mic=mic, backend=backend,
                executor=calls.append, recognition_timeout=0.02)
        assert mic.closed == 1 and controls.closed and calls == [] and backend.closed == 0
    finally:
        unblock.set()
        assert model_closed.wait(1)
    assert backend.closed == 1


def test_release_during_delayed_open_ignores_late_frames_and_waits_for_confirmed_close(tmp_path, loaded):
    class ReleaseDuringOpen(FakeHotkeys):
        released = False
        def held(self):
            if mic.open_entered.is_set():
                self.released = True
                self.is_held = False
            return self.is_held
        def available(self):
            if self.released:
                mic.allow_open.set()  # controller has cleared active before cleanup polling
            return True
    controls = ReleaseDuringOpen()
    mic = DelayedNativeMic(controls, open_allowed=False)
    try:
        session = run(tmp_path, loaded, controls=controls, mic=mic, cleanup_timeout=0.2)
        assert session.report.last_result["status"] == "cancelled"
        assert session.report.last_result["captured_frames"] == 0
        assert session.report.last_result["capture_closed"] and not session.calls
        assert "Ready — speak" not in session.output
        assert mic.closed == 1 and not ptt._CAPTURE_UNSAFE.is_set()
    finally:
        mic.unblock()


def test_lock_during_delayed_open_discards_callbacks_after_lock_and_closes(tmp_path, loaded):
    class LockDuringOpen(FakeHotkeys):
        locked = False
        def available(self):
            if self.locked:
                mic.allow_open.set()  # cleanup still runs after the lock was observed
            elif mic.open_entered.is_set():
                self.locked = True
            return not self.locked
    controls = LockDuringOpen()
    mic, calls = DelayedNativeMic(controls, open_allowed=False, release=False), []
    try:
        with pytest.raises(PttError, match="locked"):
            run(tmp_path, loaded, controls=controls, mic=mic,
                executor=calls.append, cleanup_timeout=0.2)
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["captured_frames"] == 0 and failure["capture_closed"]
        assert calls == [] and controls.closed and mic.closed == 1
    finally:
        mic.unblock()


def test_ctrl_c_during_delayed_open_still_closes_and_rejects_late_callbacks(tmp_path, loaded):
    class InterruptDuringOpen(FakeHotkeys):
        interrupted = False
        def held(self):
            if mic.open_entered.is_set():
                self.interrupted = True
                raise KeyboardInterrupt
            return True
        def available(self):
            if self.interrupted:
                mic.allow_open.set()
            return True
    controls = InterruptDuringOpen()
    mic = DelayedNativeMic(controls, open_allowed=False)
    try:
        with pytest.raises(KeyboardInterrupt):
            run(tmp_path, loaded, controls=controls, mic=mic, cleanup_timeout=0.2)
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["captured_frames"] == 0 and failure["capture_closed"]
        assert controls.closed and mic.closed == 1 and not ptt._CAPTURE_UNSAFE.is_set()
    finally:
        mic.unblock()


def test_first_frame_startup_deadline_closes_an_open_but_silent_driver(tmp_path, loaded):
    controls = FakeHotkeys()
    mic, calls = DelayedNativeMic(controls, frames=False, release=False), []
    try:
        with pytest.raises(PttError, match="startup timed out"):
            run(tmp_path, loaded, controls=controls, mic=mic, executor=calls.append,
                startup_timeout=0.02, cleanup_timeout=0.2)
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["captured_frames"] == 0 and failure["capture_closed"]
        assert not failure["process_exit_required"] and not ptt._CAPTURE_UNSAFE.is_set()
        assert calls == [] and mic.closed == 1 and controls.closed
    finally:
        mic.unblock()


@pytest.mark.parametrize("blocked_operation", ["open", "close"])
def test_hung_native_operation_has_bounded_cleanup_and_permanent_process_boundary(tmp_path, loaded, blocked_operation):
    controls = FakeHotkeys()
    mic = DelayedNativeMic(controls, open_allowed=blocked_operation != "open",
                           close_allowed=blocked_operation != "close")
    calls = []
    try:
        with pytest.raises(PttCleanupError, match="Exit this process"):
            run(tmp_path, loaded, controls=controls, mic=mic, executor=calls.append,
                startup_timeout=0.02, cleanup_timeout=0.02)
        assert not mic.finished.is_set() and controls.closed and calls == []
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["process_exit_required"] and failure["capture_closed"] is not True
        assert not failure["executed"] and ptt._CAPTURE_UNSAFE.is_set()
        another = FakeHotkeys()
        with pytest.raises(PttCleanupError, match="previous microphone"):
            run(tmp_path, loaded, controls=another)
        assert not another.registered
    finally:
        mic.unblock()
    # Late driver completion cannot permit another command in this process.
    assert ptt._CAPTURE_UNSAFE.is_set()
    if blocked_operation == "open":
        assert failure["captured_frames"] == 0


def test_lock_is_still_observed_while_native_close_waits(tmp_path, loaded):
    class LockDuringClose(FakeHotkeys):
        def available(self):
            if mic.close_entered.is_set():
                self.unlocked = False
                mic.allow_close.set()
            return self.unlocked
    controls = LockDuringClose()
    mic, calls = DelayedNativeMic(controls, close_allowed=False), []
    try:
        with pytest.raises(PttError, match="locked"):
            run(tmp_path, loaded, controls=controls, mic=mic,
                executor=calls.append, cleanup_timeout=0.2)
        assert controls.closed and mic.closed == 1 and calls == []
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["capture_closed"] and not failure["executed"]
        assert not ptt._CAPTURE_UNSAFE.is_set()
    finally:
        mic.unblock()


@pytest.mark.parametrize("cleanup_completes", [True, False])
def test_ctrl_c_interrupting_native_cleanup_wait_preserves_closure_boundary(
    tmp_path, loaded, monkeypatch, cleanup_completes
):
    controls = FakeHotkeys()
    mic = DelayedNativeMic(controls, close_allowed=False)
    original_wait, interrupted = threading.Event.wait, False
    # Model the cleanup deadline independently of OS thread scheduling. The
    # success branch must confirm the capture worker's own completion event;
    # allowing native close does not prove it finished inside a 20 ms budget.
    clock = [100.0]
    monkeypatch.setattr(ptt.time, "perf_counter", lambda: clock[0])
    def interrupted_wait(event, timeout=None):
        nonlocal interrupted
        if (threading.get_ident() == threading.main_thread().ident
                and mic.close_entered.is_set() and not interrupted):
            interrupted = True
            if cleanup_completes:
                mic.allow_close.set()
                # This is the controller's capture_finished.wait. Bypass our
                # injected interruption while synchronizing with the worker's
                # final closure bookkeeping, not merely the driver's release.
                assert original_wait(event, 1), "Capture worker did not confirm cleanup"
                assert mic.finished.is_set() and mic.closed == 1 and not mic.is_open
            else:
                # Advance past the unchanged cleanup_timeout while native
                # close remains blocked; the process boundary must stay shut.
                clock[0] += 0.03
            raise KeyboardInterrupt
        return original_wait(event, timeout)
    monkeypatch.setattr(threading.Event, "wait", interrupted_wait)
    expected = KeyboardInterrupt if cleanup_completes else PttCleanupError
    try:
        with pytest.raises(expected):
            run(tmp_path, loaded, controls=controls, mic=mic, cleanup_timeout=0.02)
        assert interrupted and controls.closed
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert not failure["executed"] and failure["process_exit_required"] != cleanup_completes
        if cleanup_completes:
            assert failure["capture_closed"] and not ptt._CAPTURE_UNSAFE.is_set()
        else:
            assert failure["capture_closed"] is not True and ptt._CAPTURE_UNSAFE.is_set()
            with pytest.raises(PttCleanupError, match="previous microphone"):
                run(tmp_path, loaded)
    finally:
        mic.unblock()


def test_exception_from_lock_probe_during_cleanup_does_not_skip_native_close(tmp_path, loaded):
    class FailingProbe(FakeHotkeys):
        failed = False
        def available(self):
            if mic.close_entered.is_set() and not self.failed:
                self.failed = True
                mic.allow_close.set()
                raise RuntimeError("lock probe failed during cleanup")
            return True
    controls = FailingProbe()
    mic = DelayedNativeMic(controls, close_allowed=False)
    try:
        with pytest.raises(RuntimeError, match="lock probe failed"):
            run(tmp_path, loaded, controls=controls, mic=mic, cleanup_timeout=0.2)
        failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
        assert failure["capture_closed"] and not failure["executed"]
        assert mic.closed == 1 and controls.closed and not ptt._CAPTURE_UNSAFE.is_set()
    finally:
        mic.unblock()


def test_callback_during_native_close_cannot_add_post_release_audio(tmp_path, loaded):
    controls = FakeHotkeys()
    mic = DelayedNativeMic(controls, late_close_frame=True)
    session = run(tmp_path, loaded, controls=controls, mic=mic)
    assert session.report.last_result["captured_frames"] == 12 * 512
    assert session.report.last_result["capture_closed"] and mic.closed == 1


def test_ctrl_c_while_waiting_cleans_up_without_capture(tmp_path, loaded):
    class Interrupted(FakeHotkeys):
        def wait_pressed(self, stop):
            raise KeyboardInterrupt
    controls, backend = Interrupted(), FakeBackend()
    mic = FakeMicrophone(controls)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend)
    assert controls.closed and backend.closed == 1 and mic.opened == 0


def test_ctrl_c_during_capture_closes_stream_before_shutdown(tmp_path, loaded):
    class Interrupted(FakeHotkeys):
        capturing = False
        def held(self):
            if self.capturing:
                raise KeyboardInterrupt
            return True
    controls, backend = Interrupted(), FakeBackend()
    class InterruptMic(FakeMicrophone):
        @contextmanager
        def open(self, callback):
            with super().open(callback) as device:
                controls.capturing = True
                yield device
    mic = InterruptMic(controls)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, loaded, controls=controls, mic=mic, backend=backend)
    assert mic.closed == 1 and controls.closed and backend.closed == 1
    failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
    assert failure["error_type"] == "KeyboardInterrupt" and failure["capture_closed"]
    assert failure["executed"] is False


def test_executor_exception_logs_unknown_mutation_outcome_and_keeps_capture_closed(tmp_path, loaded):
    def broken_executor(plan):
        raise RuntimeError("executor failed after entering")
    with pytest.raises(RuntimeError, match="executor failed"):
        run(tmp_path, loaded, executor=broken_executor)
    failure = json.loads(next((tmp_path / "logs").glob("ptt-*.jsonl")).read_text())
    assert failure["capture_closed"] and failure["execution_attempted"]
    assert failure["executed"] is None


def test_locked_capture_and_lock_before_execution_both_fail_closed(tmp_path, loaded):
    controls = FakeHotkeys()
    class LockMic(FakeMicrophone):
        @contextmanager
        def open(self, callback):
            with super().open(callback) as device:
                controls.unlocked = False
                yield device
    mic, calls = LockMic(controls), []
    with pytest.raises(PttError, match="locked"):
        run(tmp_path, loaded, controls=controls, mic=mic, executor=calls.append)
    assert calls == [] and mic.closed == 1 and controls.closed
    controls = FakeHotkeys()
    mic = FakeMicrophone(controls)
    with pytest.raises(PttError, match="locked before execution"):
        run(tmp_path, loaded, controls=controls, mic=mic, executor=calls.append,
            state_reader=lambda: setattr(controls, "unlocked", False))
    assert calls == [] and mic.closed == 1


def test_last_app_comes_from_current_execution_state_and_reuses_loaded_backend(tmp_path, loaded):
    controls, backend = FakeHotkeys(holds=2), FakeBackend(["open Notepad", "make it full screen"])
    mic, calls, state = FakeMicrophone(controls), [], {"last": None}
    def execute(plan):
        calls.append(plan)
        if plan[0]["action"] == "open_app":
            state["last"] = plan[0]["app"]
        return [{"ok": True}]
    report = listen_ptt(tmp_path, loaded, hotkeys=controls, microphone=mic,
                        backend_factory=lambda *args, **kwargs: backend,
                        executor=execute, state_reader=lambda: state["last"], output_fn=lambda text: None)
    assert report.utterances == 2 and backend.closed == 1
    assert calls[1] == [{"action": "window", "op": "maximize", "target": "Notepad"}]


def test_literal_named_loose_ptt_calls_resolver_with_raw_text_and_context(tmp_path, loaded):
    calls = []
    text = "Hey, Computer, bring it to the front"
    expected = [{"action": "switch_app", "app": "Notepad"}]

    def resolve(text, catalog, context):
        calls.append((text, catalog, context))
        return expected

    session = run(tmp_path, loaded, backend=FakeBackend([text]), resolver=resolve,
                  state_reader=lambda: "Notepad")
    assert calls == [(text, loaded.catalog, {"names": ["computer"], "last_app": "Notepad"})]
    assert session.calls == [expected]
    result = session.report.last_result
    assert result["tier"] == "tier2" and result["reason"] == "run"
    assert result["event"] == "decision" and result["capture_closed"]
    assert result["timings"]["resolver_ms"] >= 0


@pytest.mark.parametrize("text,expected", [
    ("open Notepad", [{"action": "open_app", "app": "Notepad"}]),
    ("computer open Notepad", [{"action": "open_app", "app": "Notepad"}]),
    ("bring it to the front", None),
    ("someone said computer bring it to the front", None),
    ("computer", None),
])
def test_unnamed_ptt_and_named_tier_one_bypass_selector(tmp_path, loaded, text, expected):
    def forbidden(*args):
        pytest.fail("Only named unresolved PTT may reach tier 2")

    session = run(tmp_path, loaded, backend=FakeBackend([text]), resolver=forbidden)
    assert session.report.last_result["plan"] == expected
    assert session.calls == ([expected] if expected is not None else [])
    assert session.report.last_result["tier"] == "tier1"


@pytest.mark.parametrize("outcome", [None, "garbage", [{"action": "shell"}], "raise"])
def test_ptt_optional_resolver_failure_does_not_execute_or_raise(tmp_path, loaded, outcome):
    calls = []

    def resolve(*args):
        calls.append(args)
        if outcome == "raise":
            raise ConnectionError("fake stopped server")
        return outcome

    session = run(tmp_path, loaded, backend=FakeBackend(["computer get calculator going"]),
                  resolver=resolve)
    assert len(calls) == 1 and session.calls == []
    assert session.report.last_result["plan"] is None
    assert session.report.last_result["reason"] == "unresolved"


def test_ptt_uses_configured_spoken_names(tmp_path, loaded):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("names: [jarvis]\n", encoding="utf-8")
    calls = []

    def resolve(text, catalog, context):
        calls.append((text, context))
        return [{"action": "open_app", "app": "Calculator"}]

    session = run(tmp_path, loaded, backend=FakeBackend(["Jarvis, get calculator going"]),
                  resolver=resolve)
    assert calls == [("Jarvis, get calculator going", {"names": ["jarvis"], "last_app": None})]
    assert session.report.last_result["tier"] == "tier2"


def test_default_ptt_executor_receives_pointer_monitor_context(tmp_path, loaded, monkeypatch):
    controls, backend = FakeHotkeys(), FakeBackend(["computer put this window on the other screen"])
    mic, executions = FakeMicrophone(controls), []
    move = [{"action": "window", "op": "move_monitor", "target": "active", "monitor": "next"}]

    def execute(root, current_catalog, plan, *, monitor_reference):
        executions.append((plan, monitor_reference))
        return PttExecution([{"ok": True}])

    monkeypatch.setattr(ptt, "_default_execute", execute)
    report = listen_ptt(tmp_path, loaded, hotkeys=controls, microphone=mic,
                        backend_factory=lambda *args, **kwargs: backend,
                        resolver=lambda *args: move, state_reader=lambda: None,
                        output_fn=lambda text: None)
    assert report.last_result["plan"] == move
    assert executions == [(move, "cursor")]


def test_hotkey_cleanup_still_runs_if_model_close_raises(tmp_path, loaded):
    class CloseFailure(FakeBackend):
        def close(self):
            raise RuntimeError("close failed")
    controls = FakeHotkeys(holds=0)
    with pytest.raises(RuntimeError, match="close failed"):
        run(tmp_path, loaded, controls=controls, backend=CloseFailure())
    assert controls.closed


@pytest.mark.parametrize("kwargs", [
    {"max_seconds": 21}, {"max_seconds": 0}, {"backlog_seconds": 6},
    {"recognition_timeout": float("nan")}, {"max_utterances": 0},
    {"startup_timeout": 0}, {"cleanup_timeout": 31},
])
def test_limits_are_validated_before_registration(tmp_path, loaded, kwargs):
    controls = FakeHotkeys()
    with pytest.raises(PttError):
        run(tmp_path, loaded, controls=controls, **kwargs)
    assert not controls.registered
