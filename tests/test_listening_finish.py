"""Timed dry-run completion uses real VAD endpoints and in-process fake devices."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar import listening
from voicebar.audio import FRAME_SAMPLES, SpeechSegmenter
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.gate import Gate
from voicebar.stt.base import SAMPLE_RATE, TextEvent


class FinishRig:
    """One callback can contain speech on both sides of the measured endpoint."""

    def __init__(self, tmp_path, monkeypatch, frames, *, deadline_frames=2,
                 dry_run=True, session_dry_run=True, max_seconds=20):
        monkeypatch.setattr(listening, "_UNSAFE", threading.Event())
        self.clock = [0.0]
        monkeypatch.setattr(listening.time, "perf_counter", lambda: self.clock[0])
        self.loaded = LoadedCatalog(Catalog(apps=["Notepad", "Calculator"]), [], {}, ["Notepad"])
        self.rows, self.calls, self.decoded = [], [], []
        self.processed, self.closed, self.entered = (threading.Event() for _ in range(3))
        self.stop = threading.Event()
        self.on_available = self.on_decode = lambda: None
        self.event_hook = lambda row: None
        self.extra_callbacks = []
        self.pcm = np.repeat(np.asarray(frames, dtype="<i2"), FRAME_SAMPLES).tobytes()
        self.minutes = deadline_frames * FRAME_SAMPLES / SAMPLE_RATE / 60
        self.dry_run = dry_run
        self.tmp_path = tmp_path
        rig = self

        class Vad:
            def reset(self):
                pass

            def probability(self, frame):
                return float(np.any(frame))

        class Segmenter(SpeechSegmenter):
            def feed(self, audio, at):
                result = super().feed(audio, at)
                rig.processed.set()
                return result

            def flush(self):
                pytest.fail("Live completion must never flush or pad a partial phrase")

        class Microphone:
            is_open = False
            opens = 0

            @contextmanager
            def open(self, callback):
                self.opens += 1
                self.is_open = True
                rig.receive = callback
                try:
                    rig.clock[0] = len(rig.pcm) / 2 / SAMPLE_RATE
                    callback(rig.pcm, len(rig.pcm) // 2, "")
                    for pcm in rig.extra_callbacks:
                        callback(pcm, len(pcm) // 2, "")
                    yield {"name": "fake microphone"}
                finally:
                    self.is_open = False
                    rig.closed.set()

        class Backend:
            closed = False

            def transcribe_segment(self, audio, emit):
                rig.decoded.append(audio.copy())
                rig.entered.set()
                # Recognition is in flight while the capture context closes.
                # A controller based on an empty queue would revoke it here.
                assert rig.closed.wait(3), "Sealed capture did not close"
                rig.on_decode()
                emit(TextEvent(str(len(rig.decoded)), "computer open Notepad", True,
                               0, len(audio), rig.clock[0], "fake"))

            def close(self):
                self.closed = True

        self.segmenter = Segmenter(Vad(), silence_seconds=FRAME_SAMPLES / SAMPLE_RATE,
                                   pre_roll_seconds=0, max_seconds=max_seconds)
        self.microphone, self.backend = Microphone(), Backend()

        def log(row):
            self.rows.append(row)
            self.event_hook(row)

        self.session = listening.GateSession(
            Gate(self.loaded.catalog, ["computer"]), dry_run=session_dry_run,
            execute=lambda plan: self.calls.extend(plan) or [{"ok": True}],
            log=log, clock=lambda: self.clock[0],
        )
        checks = [0]

        def available():
            checks[0] += 1
            if checks[0] > 1:
                assert self.processed.wait(3), "Audio worker did not process its callback"
                self.on_available()
            return True

        self.controls = SimpleNamespace(available=available)

    def run(self):
        return listening.listen(
            self.tmp_path, self.loaded, backend_name="fake", backend=self.backend,
            microphone=self.microphone, segmenter=self.segmenter, session=self.session,
            controls=self.controls, stop=self.stop, dry_run=self.dry_run,
            minutes=self.minutes, output=lambda _: None,
        )[0].outcome


def test_tail_command_finishes_whole_and_later_speech_is_outside_measurement(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000, 0, 3000, 3000, 0])
    rig.on_decode = lambda: pytest.fail("Draining revoked dry-run authorization") if (
        not rig.session.capture_allowed) else None
    outcome = rig.run()
    assert outcome["complete"] and outcome["coverage_complete"]
    assert outcome["stop_reason"] == "duration" and not outcome["failures"]
    assert outcome["measurement_end"] == pytest.approx(3 * FRAME_SAMPLES / SAMPLE_RATE)
    assert outcome["measurement_seconds"] == pytest.approx(3 * FRAME_SAMPLES / SAMPLE_RATE)
    assert outcome["measurement_end"] > outcome["finishing"]["minimum_end"]
    assert outcome["finishing"]["producer_done"] and outcome["finishing"]["recognition_drained"]
    assert outcome["excluded_samples"] == 3 * FRAME_SAMPLES
    assert len(rig.decoded) == 1 and len(rig.decoded[0]) == 3 * FRAME_SAMPLES
    assert np.all(rig.decoded[0][:2 * FRAME_SAMPLES] > 0)
    assert np.all(rig.decoded[0][2 * FRAME_SAMPLES:] == 0)
    assert outcome["decision_count"] == 1 and len(outcome["would_run"]) == 1
    assert outcome["would_run"][0]["text"] == "computer open Notepad"
    assert not rig.calls and rig.backend.closed and not rig.microphone.is_open
    assert rig.microphone.opens == 1
    assert any(row.get("reason") == "measurement_sealed" for row in rig.rows)


def test_quiet_stream_startup_gap_does_not_count_toward_requested_capture_duration(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [0], deadline_frames=2)
    old_available = rig.controls.available
    checks = [0]
    first_sample = 1.5 * FRAME_SAMPLES / SAMPLE_RATE

    @contextmanager
    def open_delayed(callback):
        rig.microphone.opens += 1
        rig.microphone.is_open = True
        rig.receive = callback
        try:
            rig.clock[0] = first_sample + FRAME_SAMPLES / SAMPLE_RATE
            callback(rig.pcm, FRAME_SAMPLES, "")
            yield {"name": "delayed fake microphone"}
        finally:
            rig.microphone.is_open = False
            rig.closed.set()

    def available():
        old_available()
        checks[0] += 1
        if checks[0] == 2:
            # The former began + N deadline is already reached, but only half
            # the requested duration has arrived from the delayed stream.
            rig.clock[0] = first_sample + FRAME_SAMPLES / SAMPLE_RATE
            assert rig.segmenter.sealed_at is None
            assert not any(row.get("reason") == "finishing" for row in rig.rows)
            rig.clock[0] = first_sample + 2 * FRAME_SAMPLES / SAMPLE_RATE
            rig.receive(rig.pcm, FRAME_SAMPLES, "")
        return True

    rig.microphone.open, rig.controls.available = open_delayed, available
    outcome = rig.run()
    assert outcome["complete"] and outcome["decision_count"] == 0
    assert outcome["capture_start_offset_seconds"] == pytest.approx(first_sample)
    assert outcome["measurement_start"] == pytest.approx(first_sample)
    assert outcome["measurement_seconds"] == pytest.approx(2 * FRAME_SAMPLES / SAMPLE_RATE)
    assert outcome["finishing"]["minimum_end"] == pytest.approx(
        first_sample + 2 * FRAME_SAMPLES / SAMPLE_RATE)
    assert outcome["measurement_end"] == outcome["finishing"]["minimum_end"]
    assert not rig.decoded and not rig.calls and rig.microphone.opens == 1


def test_timed_dry_run_without_any_audio_uses_incomplete_fallback(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [0], deadline_frames=2)
    checks = [0]

    def locked():
        checks[0] += 1
        if checks[0] > 1:
            rig.clock[0] = .064
        return False

    rig.controls.available = locked
    outcome = rig.run()
    assert outcome["stop_reason"] == "duration" and not outcome["complete"]
    assert not outcome["valid_frame_seen"] and outcome["capture_suspended"]
    assert outcome["measurement_start"] is None and outcome["measurement_end"] is None
    assert not outcome["coverage_complete"] and not outcome["finishing"]["recognition_drained"]
    assert not rig.microphone.opens and not rig.decoded and not rig.calls


def test_queued_and_in_flight_full_segments_are_all_drained_before_complete(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 0, 3000, 0, 3000, 0], deadline_frames=5)
    outcome = rig.run()
    assert outcome["complete"] and outcome["decision_count"] == 3
    assert len(outcome["would_run"]) == len(rig.decoded) == 3
    assert outcome["excluded_samples"] == 0
    assert not outcome["pending"]["segments"] and not outcome["pending"]["recognition_in_flight"]
    assert not outcome["shutdown_discarded_recognition"]
    assert not rig.calls


def test_callbacks_queued_after_the_sample_cutoff_are_counted_outside_measurement(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000, 0])
    rig.extra_callbacks = [np.full(3 * FRAME_SAMPLES, 3000, dtype="<i2").tobytes()]
    outcome = rig.run()
    assert outcome["complete"] and outcome["decision_count"] == 1
    assert outcome["excluded_samples"] == 3 * FRAME_SAMPLES
    assert len(rig.decoded) == 1 and len(rig.decoded[0]) == 3 * FRAME_SAMPLES
    assert not outcome["pending"]["audio_frames"] and not outcome["shutdown_discarded_audio_frames"]
    assert rig.microphone.opens == 1 and not rig.calls


def test_no_natural_endpoint_times_out_without_flushing_a_partial_command(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000], deadline_frames=1)
    rig.on_available = lambda: rig.clock.__setitem__(0, .032 + 20.001)
    with pytest.raises(listening.ListeningFailure) as caught:
        rig.run()
    outcome = caught.value.outcome
    assert outcome["stop_reason"] == "finish_timeout"
    assert not outcome["complete"] and not outcome["coverage_complete"]
    assert outcome["measurement_end"] is None and outcome["pending"]["active_speech"]
    assert outcome["failures"][0]["phase"] == "finish_timeout"
    assert "endpoint" in outcome["failures"][0]["message"]
    assert outcome["would_run"] == [] and not rig.decoded and not rig.calls
    assert rig.backend.closed and not rig.microphone.is_open


def test_model_drain_has_the_same_bounded_finishing_deadline(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000, 0])
    release = threading.Event()

    def decode():
        assert release.wait(3), "Deadline shutdown did not release fake recognition"

    def available():
        assert rig.entered.wait(3), "Recognition did not begin"
        rig.clock[0] = .064 + 20.001

    def log(row):
        if row.get("reason") == "control" and row.get("state") == "off":
            release.set()

    rig.on_decode, rig.on_available, rig.event_hook = decode, available, log
    try:
        with pytest.raises(listening.ListeningFailure) as caught:
            rig.run()
    finally:
        release.set()
    outcome = caught.value.outcome
    assert outcome["stop_reason"] == "finish_timeout" and not outcome["complete"]
    assert "recognition" in outcome["failures"][0]["message"]
    assert outcome["measurement_end"] is not None
    assert outcome["shutdown_discarded_recognition"] == 1
    assert outcome["would_run"] == [] and not rig.calls
    assert not outcome["cleanup"]["workers_alive"] and rig.backend.closed


@pytest.mark.parametrize("ending", ["external_stop", "locked", "off", "interrupted", "capture_failure"])
def test_hard_stops_interrupt_endpoint_finishing_immediately(tmp_path, monkeypatch, ending):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000], deadline_frames=1)

    def log(row):
        if row.get("reason") != "finishing":
            return
        if ending == "external_stop":
            rig.stop.set()
        elif ending == "locked":
            rig.session.control(locked=True)
        elif ending == "off":
            rig.session.control(enabled=False)
        elif ending == "interrupted":
            raise KeyboardInterrupt()
        else:
            rig.receive(b"bad", 512, "fake capture error")

    rig.event_hook = log
    if ending in ("interrupted", "capture_failure"):
        with pytest.raises(listening.ListeningFailure) as caught:
            rig.run()
        outcome = caught.value.outcome
    else:
        outcome = rig.run()
    expected = {"locked": "capture_suspended", "off": "capture_suspended",
                "capture_failure": "worker_failure"}.get(ending, ending)
    assert outcome["stop_reason"] == expected
    assert outcome["finishing"]["started_at"] is not None
    assert not outcome["complete"] and not outcome["coverage_complete"]
    assert outcome["measurement_end"] is None
    assert not outcome["cleanup"]["workers_alive"]
    assert rig.microphone.opens == 1 and not rig.microphone.is_open and rig.backend.closed
    assert outcome["would_run"] == [] and not rig.calls and not rig.decoded
    if ending in ("locked", "off"):
        assert outcome["capture_suspended"]


@pytest.mark.parametrize("ending", ["external_stop", "locked", "off", "recognition_failure"])
def test_hard_stops_during_model_drain_never_accept_the_late_plan(tmp_path, monkeypatch, ending):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000, 0])

    def decode():
        if ending == "external_stop":
            rig.stop.set()
        elif ending == "locked":
            rig.session.control(locked=True)
        elif ending == "off":
            rig.session.control(enabled=False)
        else:
            raise RuntimeError("fake model drain failure")

    # Keep the controller from restoring a fake lock before the stale transcript
    # has been assessed. The actual gate generation check remains in use.
    original_hear = rig.session.hear

    def hear(*args, **kwargs):
        row = original_hear(*args, **kwargs)
        rig.stop.set()
        return row

    rig.session.hear, rig.on_decode = hear, decode
    if ending == "recognition_failure":
        with pytest.raises(listening.ListeningFailure) as caught:
            rig.run()
        outcome = caught.value.outcome
        assert outcome["stop_reason"] == "worker_failure"
        assert outcome["failures"][0]["phase"] == "recognition"
    else:
        outcome = rig.run()
        assert outcome["reason_counts"] == {"stale_or_closed": 1}
    assert not outcome["complete"] and not outcome["would_run"] and not rig.calls
    assert not outcome["cleanup"]["workers_alive"] and rig.backend.closed
    assert not rig.microphone.is_open and not rig.session.capture_allowed


@pytest.mark.parametrize("dry_run,session_dry_run", [(False, True), (True, False), (False, False)])
def test_endpoint_extension_requires_both_dry_run_authorizations(
    tmp_path, monkeypatch, dry_run, session_dry_run
):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000], deadline_frames=1,
                    dry_run=dry_run, session_dry_run=session_dry_run)
    outcome = rig.run()
    assert outcome["stop_reason"] == "duration" and not outcome["complete"]
    assert not outcome["finishing"]["allowed"] and outcome["finishing"]["started_at"] is None
    assert outcome["measurement_end"] is None and rig.segmenter.sealed_at is None
    assert not rig.calls and not rig.decoded and not rig.microphone.is_open


def test_overlong_dropped_phrase_cannot_be_reported_as_complete_coverage(tmp_path, monkeypatch):
    rig = FinishRig(tmp_path, monkeypatch, [3000, 3000, 3000, 0], deadline_frames=2,
                    max_seconds=.09)
    outcome = rig.run()
    assert outcome["stop_reason"] == "duration" and not outcome["complete"]
    assert not outcome["coverage_complete"] and not outcome["pending"]["dropping"]
    assert outcome["audio_drops"][0]["reason"] == "maximum_duration"
    assert outcome["decision_count"] == 0 and not outcome["would_run"]
    assert outcome["finishing"]["recognition_drained"] and not rig.decoded
