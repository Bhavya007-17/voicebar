"""Lifecycle regressions use only in-process fakes and event handshakes."""

import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from voicebar import listening
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.gate import Gate
from voicebar.stt.base import SAMPLE_RATE, TextEvent


@pytest.fixture
def loaded(monkeypatch):
    # A deliberately failed cleanup must not poison unrelated fake tests.
    monkeypatch.setattr(listening, "_UNSAFE", threading.Event())
    return LoadedCatalog(Catalog(apps=["Notepad", "Calculator"]), [], {}, ["Notepad"])


def test_shutdown_revokes_a_transcription_already_in_progress(tmp_path, loaded):
    stop, finishing, release, revoked = (threading.Event() for _ in range(4))
    rows, calls, errors = [], [], []

    def record(row):
        rows.append(row)
        if row.get("reason") == "control" and row.get("state") == "off":
            revoked.set()

    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]),
        execute=lambda plan: calls.extend(plan) or [{"ok": True}], log=record,
    )

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None

        def reset(self):
            self.last_speech_at = None

        def feed(self, audio, at):
            self.last_speech_at = at + len(audio) / SAMPLE_RATE
            return [SimpleNamespace(audio=audio, start=at,
                                    end=self.last_speech_at, speech_end=self.last_speech_at)]

        def drain_drops(self):
            return []

    class Backend:
        closed = False

        def start(self, emit):
            self.emit = emit

        def feed(self, audio):
            pass

        def finish(self):
            finishing.set()
            assert release.wait(3), "Test did not release the fake recognizer"
            self.emit(TextEvent("one", "computer open Notepad", True, 0, 512,
                                time.perf_counter(), "fake"))

        def close(self):
            self.closed = True

    microphone, backend = Microphone(), Backend()

    def run():
        try:
            listening.listen(
                tmp_path, loaded, backend_name="fake", backend=backend,
                microphone=microphone, segmenter=Segmenter(), session=session,
                controls=SimpleNamespace(available=lambda: True), stop=stop,
                output=lambda _: None,
            )
        except BaseException as error:  # noqa: BLE001 - transfer fake worker failures to test thread
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert finishing.wait(3), "Fake recognition did not reach its handshake"
        stop.set()
        assert revoked.wait(3), "Shutdown did not revoke pending speech"
        release.set()
    finally:
        stop.set()
        release.set()
        worker.join(3)

    assert not worker.is_alive() and not errors
    assert not calls, "Speech completed after shutdown and reached the executor"
    assert backend.closed and not microphone.is_open
    assert any(row.get("reason") == "stale_or_closed" for row in rows)


def test_backend_close_failure_poisoned_process_and_stopped_diagnostics(tmp_path, loaded):
    stop = threading.Event()
    rows = []
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"),
        log=rows.append,
    )

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                stop.set()
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    def fail_close():
        raise RuntimeError("fake native model cleanup failed")

    microphone = Microphone()
    with pytest.raises(RuntimeError, match="fake native model cleanup failed"):
        listening.listen(
            tmp_path, loaded, backend_name="fake",
            backend=SimpleNamespace(close=fail_close), microphone=microphone,
            segmenter=SimpleNamespace(reset=lambda: None), session=session,
            controls=SimpleNamespace(available=lambda: True), stop=stop,
            output=lambda _: None,
        )

    assert not microphone.is_open and not session.capture_allowed
    assert listening._UNSAFE.is_set(), "Unconfirmed native cleanup must forbid same-process retry"
    assert any(row.get("reason") == "stopped" for row in rows)


def test_original_queue_failure_survives_blocked_cleanup_and_freezes_report(tmp_path, loaded):
    finishing, release, recognition_returned = (threading.Event() for _ in range(3))
    rows, calls = [], []
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda plan: calls.extend(plan),
        dry_run=True, log=rows.append,
    )
    session.hear("computer open Notepad", start=0, end=0, speech_end=0,
                 generation=session.generation)

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(512 * 2), 512, "")
                assert finishing.wait(3), "Recognition did not reach its handshake"
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None
        feeds = 0

        def reset(self):
            pass

        def feed(self, audio, at):
            self.feeds += 1
            self.last_speech_at = at + len(audio) / SAMPLE_RATE
            block = SimpleNamespace(audio=audio, start=at, end=self.last_speech_at,
                                    speech_end=self.last_speech_at)
            # Recognition owns the first block. Nine more must exceed the
            # unchanged eight-segment queue while it remains blocked.
            return [block] * (1 if self.feeds == 1 else 9)

        def drain_drops(self):
            return []

    class Backend:
        closed = False

        def start(self, emit):
            self.emit = emit

        def feed(self, audio):
            pass

        def finish(self):
            finishing.set()
            assert release.wait(10), "Test did not release recognition"
            self.emit(TextEvent("one", "computer open Notepad", True, 0, 512,
                                time.perf_counter(), "fake"))

        def close(self):
            self.closed = True

    backend, microphone = Backend(), Microphone()

    def output(value):
        if value.startswith("{"):
            recognition_returned.set()

    try:
        with pytest.raises(listening.ListeningFailure) as caught:
            listening.listen(
                tmp_path, loaded, backend_name="fake", backend=backend,
                microphone=microphone, segmenter=Segmenter(), session=session,
                controls=SimpleNamespace(available=lambda: True), output=output,
            )
        failure = caught.value
        outcome = failure.outcome
        frozen = deepcopy(outcome)
        assert failure.__cause__ is failure.cause
        assert "Recognition backlog exceeded its bound" in str(failure.cause)
        assert "Native worker cleanup unconfirmed" in str(failure)
        assert not outcome["complete"] and outcome["stop_reason"] == "worker_failure"
        assert outcome["failures"][0]["phase"] == "audio"
        assert any(row["phase"] == "worker_cleanup" for row in outcome["failures"])
        assert outcome["cleanup"]["workers_alive"] == ["voicebar-recognition"]
        assert not outcome["cleanup"]["stream_open"] and not microphone.is_open
        assert not outcome["cleanup"]["backend_close_attempted"] and not backend.closed
        assert outcome["cleanup"]["unsafe"] and listening._UNSAFE.is_set()
        assert outcome["pending"]["segments"] == 8
        assert outcome["pending"]["recognition_in_flight"]
        assert outcome["decision_count"] == 1 and outcome["reason_counts"] == {"run": 1}
        assert outcome["would_run"][0]["text"] == "computer open Notepad"
        failure_index = next(i for i, row in enumerate(rows) if row.get("phase") == "audio")
        stopped_index = next(i for i, row in enumerate(rows) if row.get("reason") == "stopped")
        assert failure_index < stopped_index
        release.set()
        assert recognition_returned.wait(3), "Late recognition did not finish"
        assert any(row.get("reason") == "stale_or_closed" for row in rows)
        session.would_run.append({"text": "late mutation"})
        assert outcome == frozen, "Late worker/session updates changed the finalized report"
        assert not calls and not session.capture_allowed
    finally:
        release.set()


@pytest.mark.parametrize("ending", ["duration", "external_stop", "interrupted"])
def test_outcome_distinguishes_duration_external_stop_and_interrupt(tmp_path, loaded, monkeypatch, ending):
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"),
    )
    stop = threading.Event()
    consumed = threading.Event()
    clock, checks = [0.0], [0]
    if ending == "external_stop":
        stop.set()
    elif ending == "duration":
        monkeypatch.setattr(listening.time, "perf_counter", lambda: clock[0])

    def available():
        if ending == "interrupted":
            raise KeyboardInterrupt()
        checks[0] += 1
        if ending == "duration" and checks[0] > 1:
            assert consumed.wait(3), "Timed soak did not consume a valid microphone frame"
            clock[0] = 1.0
        return True

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, audio, at):
            consumed.set()
            return []

        def drain_drops(self):
            return []

    kwargs = {"backend_name": "fake", "backend": SimpleNamespace(close=lambda: None),
              "microphone": Microphone(), "segmenter": Segmenter(), "session": session,
              "controls": SimpleNamespace(available=available), "stop": stop,
              "output": lambda _: None, "minutes": .001}
    if ending == "interrupted":
        with pytest.raises(listening.ListeningFailure) as caught:
            listening.listen(tmp_path, loaded, **kwargs)
        assert isinstance(caught.value.__cause__, KeyboardInterrupt)
        assert caught.value.outcome is session.outcome
    else:
        returned, _ = listening.listen(tmp_path, loaded, **kwargs)
        assert returned is session
    outcome = session.outcome
    assert outcome["stop_reason"] == ending
    assert outcome["complete"] is (ending == "duration")
    assert outcome["status"] == {"duration": "complete", "external_stop": "stopped",
                                 "interrupted": "interrupted"}[ending]
    assert outcome["elapsed_seconds"] >= 0
    assert outcome["cleanup"]["backend_closed"]
    assert not outcome["cleanup"]["workers_alive"]
    assert outcome["valid_frame_seen"] is (ending == "duration")
    assert not outcome["capture_suspended"], "Final shutdown must not count as suspended listening"
    assert not session.capture_allowed


def test_failure_logging_failure_never_replaces_original_cleanup_error(tmp_path, loaded):
    original = RuntimeError("original model close failure")
    rows = []

    def log(row):
        if row.get("event") == "failure":
            raise OSError("fake log storage unavailable")
        rows.append(row)

    def close():
        raise original

    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"), log=log,
    )
    stop = threading.Event()
    stop.set()
    with pytest.raises(listening.ListeningFailure) as caught:
        listening.listen(
            tmp_path, loaded, backend_name="fake", backend=SimpleNamespace(close=close),
            microphone=SimpleNamespace(is_open=False), segmenter=SimpleNamespace(reset=lambda: None),
            session=session, controls=SimpleNamespace(available=lambda: True), stop=stop,
            output=lambda _: None,
        )
    failure = caught.value
    assert failure.cause is original and failure.__cause__ is original
    assert "original model close failure" in str(failure)
    assert "fake log storage unavailable" in str(failure)
    assert [row["phase"] for row in failure.outcome["failures"]] == ["backend_close", "error_log"]
    assert not failure.outcome["complete"] and failure.outcome["cleanup"]["unsafe"]
    assert not failure.outcome["cleanup"]["backend_closed"]
    assert any(row.get("reason") == "stopped" for row in rows)


def test_startup_logging_failure_keeps_outcome_and_closes_unstarted_resources(tmp_path, loaded):
    original = OSError("fake startup log failure")
    closed = []

    def log(_):
        raise original

    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"), log=log,
    )
    with pytest.raises(listening.ListeningFailure) as caught:
        listening.listen(
            tmp_path, loaded, backend_name="fake", backend=SimpleNamespace(close=lambda: closed.append(True)),
            microphone=SimpleNamespace(is_open=False), segmenter=SimpleNamespace(reset=lambda: None),
            session=session, controls=SimpleNamespace(available=lambda: True), output=lambda _: None,
        )
    failure = caught.value
    assert failure.__cause__ is original
    assert failure.outcome["stop_reason"] == "controller_failure"
    assert failure.outcome["failures"][0]["phase"] == "controller"
    assert any(row["phase"] == "error_log" for row in failure.outcome["failures"])
    assert not failure.outcome["complete"] and failure.outcome["decision_count"] == 0
    assert closed == [True] and failure.outcome["cleanup"]["backend_closed"]
    assert not failure.outcome["cleanup"]["workers_alive"]
    assert not failure.outcome["cleanup"]["unsafe"]
    assert not session.capture_allowed


@pytest.mark.parametrize("queued_segments,prior_lock", [(0, False), (1, False), (0, True)])
def test_deadline_does_not_claim_complete_after_shutdown_discards_speech(
    tmp_path, loaded, monkeypatch, queued_segments, prior_lock
):
    finishing, release = threading.Event(), threading.Event()
    clock, checks, calls = [0.0], [0], []
    rows = []
    monkeypatch.setattr(listening.time, "perf_counter", lambda: clock[0])

    def log(row):
        rows.append(row)
        if row.get("reason") == "control" and not session.enabled:
            release.set()

    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda plan: calls.extend(plan), log=log,
        dry_run=True,
    )

    def available():
        checks[0] += 1
        if checks[0] > 1:
            assert finishing.wait(3), "Recognition did not begin before the deadline"
            if prior_lock:
                session.control(locked=True)
            clock[0] = 60.0
        return not prior_lock or checks[0] == 1

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, audio, at):
            self.last_speech_at = at + len(audio) / SAMPLE_RATE
            block = SimpleNamespace(audio=audio, start=at, end=self.last_speech_at,
                                    speech_end=self.last_speech_at)
            return [block] * (1 + queued_segments)

        def drain_drops(self):
            return []

    class Backend:
        def start(self, emit):
            self.emit = emit

        def feed(self, audio):
            pass

        def finish(self):
            finishing.set()
            assert release.wait(3), "Shutdown did not revoke capture and release recognition"
            self.emit(TextEvent("one", "computer open Notepad", True, 0, 512,
                                clock[0], "fake"))

        def close(self):
            pass

    try:
        returned, _ = listening.listen(
            tmp_path, loaded, backend_name="fake", backend=Backend(), microphone=Microphone(),
            segmenter=Segmenter(), session=session, controls=SimpleNamespace(available=available),
            output=lambda _: None, minutes=1,
        )
    finally:
        release.set()
    outcome = returned.outcome
    assert outcome["stop_reason"] == "duration"
    assert not outcome["failures"] and not outcome["cleanup"]["workers_alive"]
    assert outcome["cleanup"]["backend_closed"] and not outcome["cleanup"]["stream_open"]
    assert not outcome["pending"]["recognition_in_flight"]
    assert outcome["pending"]["segments"] == queued_segments
    assert outcome["shutdown_discarded_recognition"] == (0 if prior_lock else 1)
    if not prior_lock:
        assert not outcome["complete"] and not outcome["coverage_complete"]
    assert outcome["would_run"] == [] and not calls
    assert any(row.get("reason") == "stale_or_closed" for row in rows)


def test_recognizer_revokes_external_stop_before_controller_can_notice(tmp_path, loaded):
    stop, finishing, controller_held, recognition_done = (threading.Event() for _ in range(4))
    calls, rows, checks = [], [], [0]
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda plan: calls.extend(plan) or [{"ok": True}],
        log=rows.append,
    )

    def available():
        checks[0] += 1
        if checks[0] > 1:
            assert finishing.wait(3), "Recognition did not begin"
            controller_held.set()
            assert recognition_done.wait(3), "Recognition waited for the held capture controller"
        return True

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, audio, at):
            self.last_speech_at = at + len(audio) / SAMPLE_RATE
            return [SimpleNamespace(audio=audio, start=at, end=self.last_speech_at,
                                    speech_end=self.last_speech_at)]

        def drain_drops(self):
            return []

    class Backend:
        def start(self, emit):
            self.emit = emit

        def feed(self, audio):
            pass

        def finish(self):
            finishing.set()
            assert controller_held.wait(3), "Controller did not enter its handshake"
            stop.set()
            self.emit(TextEvent("one", "computer open Notepad", True, 0, 512,
                                time.perf_counter(), "fake"))

        def close(self):
            pass

    def output(value):
        if value.startswith("{"):
            recognition_done.set()

    try:
        returned, _ = listening.listen(
            tmp_path, loaded, backend_name="fake", backend=Backend(), microphone=Microphone(),
            segmenter=Segmenter(), session=session, controls=SimpleNamespace(available=available),
            stop=stop, output=output,
        )
    finally:
        stop.set()
        controller_held.set()
        recognition_done.set()
    assert not calls, "A final transcript executed after external stop but before controller revocation"
    assert any(row.get("reason") == "stale_or_closed" for row in rows)
    assert not session.capture_allowed
    assert returned.outcome["shutdown_discarded_recognition"] == 1
    assert not returned.outcome["coverage_complete"] and not returned.outcome["complete"]


def test_default_executor_refuses_stopped_listener_before_native_resources(tmp_path, loaded, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("mode: name\n", encoding="utf-8")
    stop = threading.Event()
    stop.set()
    session, _ = listening.listen(
        tmp_path, loaded, backend_name="fake", backend=SimpleNamespace(close=lambda: None),
        microphone=SimpleNamespace(is_open=False), segmenter=SimpleNamespace(reset=lambda: None),
        controls=SimpleNamespace(available=lambda: pytest.fail("Stopped execution read desktop state")),
        stop=stop, output=lambda _: None,
    )
    monkeypatch.setattr("voicebar.executor.Context", lambda *a, **k: pytest.fail("Native execution opened"))
    results = session.execute([{"action": "open_app", "app": "Notepad"}])
    assert results == [{"ok": False, "detail": "Listening stopped; capture authorization revoked"}]
    assert not session.capture_allowed


def test_locked_entire_timed_soak_cannot_report_complete_without_audio(tmp_path, loaded, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(listening.time, "perf_counter", lambda: next(clock))
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"), dry_run=True,
    )
    returned, _ = listening.listen(
        tmp_path, loaded, backend_name="fake", backend=SimpleNamespace(close=lambda: None),
        microphone=SimpleNamespace(is_open=False, open=lambda _: pytest.fail("Locked session opened audio")),
        segmenter=SimpleNamespace(reset=lambda: None), session=session,
        controls=SimpleNamespace(available=lambda: False), minutes=.001, output=lambda _: None,
    )
    outcome = returned.outcome
    assert outcome["stop_reason"] == "duration" and not outcome["failures"]
    assert not outcome["valid_frame_seen"] and outcome["capture_suspended"]
    assert not outcome["complete"] and not outcome["coverage_complete"]
    assert not outcome["cleanup"]["stream_open"]
    assert outcome["would_run"] == [] and outcome["decision_count"] == 0


def test_lock_gap_keeps_timed_soak_incomplete_after_capture_resumes(tmp_path, loaded, monkeypatch):
    first_frame, second_frame = threading.Event(), threading.Event()
    clock, checks, feeds, opened, closed = [0.0], [0], [0], [], []
    monkeypatch.setattr(listening.time, "perf_counter", lambda: clock[0])
    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Unexpected execution"), dry_run=True,
    )

    def available():
        checks[0] += 1
        if checks[0] == 2:
            assert first_frame.wait(3), "First capture did not consume a valid frame"
            return False
        if checks[0] >= 4:
            assert second_frame.wait(3), "Capture did not resume after unlock"
            clock[0] = 1.0
        return True

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            opened.append(True)
            try:
                callback(bytes(512 * 2), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False
                closed.append(True)

    class Segmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, audio, at):
            feeds[0] += 1
            (first_frame if feeds[0] == 1 else second_frame).set()
            return []

        def drain_drops(self):
            return []

    returned, _ = listening.listen(
        tmp_path, loaded, backend_name="fake", backend=SimpleNamespace(close=lambda: None),
        microphone=Microphone(), segmenter=Segmenter(), session=session,
        controls=SimpleNamespace(available=available), minutes=.001, output=lambda _: None,
    )
    outcome = returned.outcome
    assert opened == closed == [True, True]
    assert outcome["stop_reason"] == "duration" and not outcome["failures"]
    assert outcome["valid_frame_seen"] and outcome["capture_suspended"]
    assert not outcome["complete"] and not outcome["coverage_complete"]
    assert not outcome["pending"]["audio_frames"] and not outcome["pending"]["segments"]
    assert outcome["cleanup"]["backend_closed"] and not outcome["cleanup"]["workers_alive"]
