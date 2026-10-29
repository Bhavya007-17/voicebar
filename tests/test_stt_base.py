import json
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.stt.base import (
    SttError,
    TextEvent,
    keyterms,
    read_wav,
    samples,
    transcribe_segment,
    transcribe_wav,
)
from voicebar.stt.models import require_model, sha256
from voicebar.stt.parakeet import ParakeetBackend


def wav_file(tmp_path, *, channels=1, rate=16000, width=2, frames=1600):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setframerate(rate)
        wav.setsampwidth(width)
        wav.writeframes(b"\0" * frames * channels * width)
    return path


@pytest.mark.parametrize("kwargs", [{"channels": 2}, {"rate": 48000}, {"width": 1}, {"frames": 0}])
def test_wav_rejects_unsupported_or_empty(tmp_path, kwargs):
    with pytest.raises(SttError):
        read_wav(wav_file(tmp_path, **kwargs))


@pytest.mark.parametrize("bad", [[[1, 2]], [float("nan")], [float("inf")], [1.1]])
def test_invalid_samples_fail(bad):
    with pytest.raises(SttError):
        samples(bad)


def test_wav_truncation(tmp_path):
    path = wav_file(tmp_path)
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(SttError, match="truncated"):
        read_wav(path)


def test_keyterms_preserve_installed_names_no_comma_terms():
    cat = SimpleNamespace(apps=["Claude", "A, B"], aliases={"cloud": "Claude", "Claude": "Claude"}, sites={"youtube": "url"})
    assert keyterms(cat) == ["Claude", "cloud", "youtube"]


def test_model_checks_fail_before_any_backend_import(tmp_path):
    with pytest.raises(SttError, match="scripts/setup.py"):
        require_model(tmp_path, "moonshine")
    model = tmp_path / "models" / "fake"
    model.mkdir(parents=True)
    asset = model / "weights"
    asset.write_bytes(b"real weights")
    manifest = {"models": {"fake": {"path": "fake", "files": [{"path": "weights", "size": asset.stat().st_size, "sha256": sha256(asset)}]}}}
    (tmp_path / "models/manifest.json").write_text(json.dumps(manifest))
    assert require_model(tmp_path, "fake", verify_hashes=True) == model
    asset.write_bytes(b"fake weights")
    with pytest.raises(SttError, match="checksum"):
        require_model(tmp_path, "fake", verify_hashes=True)
    manifest["models"]["fake"]["path"] = "../../outside"
    (tmp_path / "models/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SttError, match="outside"):
        require_model(tmp_path, "fake")


def test_replay_deduplicates_finals_and_never_uses_partial_for_plan(tmp_path):
    class Fake:
        name, device, hotwords_supported, partials_kind = "fake", "cpu", False, "fake"

        def start(self, emit):
            self.emit = emit

        def feed(self, chunk):
            self.emit(TextEvent("1", "wrong partial", False, 0, 100, time.perf_counter(), "fake"))

        def finish(self):
            for text in ("old final", "open notepad"):
                self.emit(TextEvent("1", text, True, 0, 1600, time.perf_counter(), "fake"))

        def close(self):
            pass

    result = transcribe_wav(Fake(), wav_file(tmp_path))
    assert result["text"] == "open notepad"
    assert result["release_to_final_ms"] is None
    assert result["last_speech_to_final_ms"] is None


def test_completed_segment_uses_explicit_backend_capability():
    events, calls = [], []
    audio = np.array([0.25, 0.5], dtype=np.float64)

    def complete(value, emit):
        calls.append(value)
        emit(TextEvent("0", "open notepad", True, 0, len(value), 0, "fake"))

    backend = SimpleNamespace(
        transcribe_segment=complete,
        start=lambda _: pytest.fail("Completed audio took the streaming path"),
    )
    transcribe_segment(backend, audio, events.append)
    assert len(calls) == 1 and calls[0].dtype == np.float32
    np.testing.assert_array_equal(calls[0], audio)
    assert [(event.text, event.final) for event in events] == [("open notepad", True)]


def test_completed_segment_falls_back_to_simple_backend_lifecycle():
    calls, events = [], []

    class Simple:
        def start(self, emit):
            calls.append("start")
            self.emit = emit

        def feed(self, audio):
            calls.append(("feed", audio.copy()))

        def finish(self):
            calls.append("finish")
            self.emit(TextEvent("0", "open notepad", True, 0, 3, 0, "fake"))

        def close(self):
            pytest.fail("Successful segment closed the reusable backend")

    transcribe_segment(Simple(), [0.1, 0.2, 0.3], events.append)
    assert calls[0] == "start" and calls[2] == "finish"
    np.testing.assert_array_equal(calls[1][1], np.array([0.1, 0.2, 0.3], dtype=np.float32))
    assert len(events) == 1 and events[0].final


def test_completed_segment_fallback_failure_closes_and_keeps_original_error():
    calls = []

    def fail(audio):
        raise SttError("recognition failed")

    backend = SimpleNamespace(start=lambda _: calls.append("start"), feed=fail,
                              finish=lambda: pytest.fail("Incomplete audio was finalized"),
                              close=lambda: calls.append("close"))
    with pytest.raises(SttError, match="recognition failed"):
        transcribe_segment(backend, [0.1], lambda event: pytest.fail("Unexpected transcript"))
    assert calls == ["start", "close"]


def test_completed_segment_invalid_audio_refuses_before_backend_lifecycle():
    backend = SimpleNamespace(transcribe_segment=lambda *a: pytest.fail("Recognition started"))
    with pytest.raises(SttError, match="normalized mono"):
        transcribe_segment(backend, [float("nan")], lambda _: None)


def test_stream_odd_frames_silence_endpoints_and_flush():
    backend = object.__new__(ParakeetBackend)
    probabilities = iter([0.0, 0.9, 0.9] + [0.0] * 12 + [0.9, 0.9])
    backend.vad = SimpleNamespace(reset=lambda: None, probability=lambda frame: next(probabilities))
    backend.model = SimpleNamespace(recognize=lambda audio, sample_rate: "open notepad")
    backend.emit = None
    events = []
    backend.start(events.append)
    audio = np.zeros(512 * 17, dtype=np.float32)
    for begin in range(0, len(audio), 173):
        backend.feed(audio[begin:begin + 173])
    backend.finish()
    assert [event.line_id for event in events if event.final] == ["0", "1"]
    assert backend.emit is None
    assert backend.audio.size == 0


def test_paced_replay_counts_inference_backlog_from_audio_clock(tmp_path, monkeypatch):
    from voicebar.stt import base
    clock = [100.0]
    monkeypatch.setattr(base.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(base.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    class Slow:
        name, device, hotwords_supported, partials_kind = "slow", "cpu", False, "fake"

        def start(self, emit):
            self.emit = emit

        def feed(self, chunk):
            clock[0] += 0.1

        def finish(self):
            self.emit(TextEvent("0", "open notepad", True, 0, 1600, clock[0], "slow"))

        def close(self):
            pass

    result = transcribe_wav(Slow(), wav_file(tmp_path), realtime=True)
    assert result["release_to_final_ms"] > 300
    assert result["last_speech_to_final_ms"] is None


def test_package_import_does_not_load_numpy_or_models():
    import subprocess
    root = Path(__file__).parents[1]
    probe = subprocess.run([sys.executable, "-c", "import sys; import voicebar.stt; assert 'numpy' not in sys.modules; assert 'moonshine_voice' not in sys.modules; assert 'onnx_asr' not in sys.modules"],
                           cwd=root, capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr
