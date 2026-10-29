"""Whisper adapter guarantees without loading models or opening audio devices."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.stt.base import SAMPLE_RATE, SttError
from voicebar.stt.whisper import REQUIRED_FILES, WhisperBackend


@pytest.fixture
def fake_whisper(tmp_path, monkeypatch):
    from voicebar.stt import whisper

    directory = tmp_path / "model"
    directory.mkdir()
    for filename in REQUIRED_FILES:
        (directory / filename).write_bytes(b"local model data")
    calls = []
    decoded = []
    state = SimpleNamespace(actual_device=None, actual_type=None, loaded=True, supported={"int8", "float16"},
                            cuda_count=1, texts=[" Open ", "Notepad."], failure=None, unloaded=0,
                            probabilities=[0.9] * 100)

    class FakeRuntime:
        def __init__(self, device, compute_type):
            self.device = state.actual_device or device
            self.compute_type = state.actual_type or ("int8_float32" if compute_type == "int8" else compute_type)
            self.device_index = [0]
            self.model_is_loaded = state.loaded

        def unload_model(self):
            state.unloaded += 1

    class FakeModel:
        max_length = 448

        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))
            self.model = FakeRuntime(kwargs["device"], kwargs["compute_type"])

        def transcribe(self, audio, **kwargs):
            decoded.append((audio.copy(), kwargs))

            def stream():
                for text in state.texts:
                    yield SimpleNamespace(text=text)
                if state.failure:
                    raise state.failure

            return stream(), SimpleNamespace(language="en")

    class FakeVad:
        def __init__(self, directory):
            self.position = 0

        def reset(self):
            self.position = 0

        def probability(self, frame):
            value = state.probabilities[self.position]
            self.position += 1
            return value

    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        get_supported_compute_types=lambda device, index: state.supported,
        get_cuda_device_count=lambda: state.cuda_count,
    ))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(whisper, "Silero", FakeVad)
    monkeypatch.setattr(whisper, "_prepare_windows_cuda", lambda: None)
    catalog = SimpleNamespace(apps=["Calculator", "Claude", "Notepad"],
                              aliases={"cloud": "Claude", "notes": "Notepad"},
                              sites={"youtube": {"url": "https://youtube.com"}})
    return SimpleNamespace(directory=directory, state=state, calls=calls, decoded=decoded, catalog=catalog,
                           create=lambda **kwargs: WhisperBackend(directory, tmp_path, catalog, **kwargs))


@pytest.mark.parametrize("name", ["whisper-large", "whisper-turbo"])
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_initialization_is_local_and_verifies_actual_placement(fake_whisper, name, device, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    backend = fake_whisper.create(name=name, device=device)
    path, options = fake_whisper.calls[0]
    assert path == str(fake_whisper.directory.resolve())
    assert options == {"device": device, "device_index": 0, "compute_type": "float16" if device == "cuda" else "int8",
                       "cpu_threads": 4, "num_workers": 1, "local_files_only": True}
    assert backend.name == name
    assert backend.runtime_details["actual_device"] == device
    assert backend.runtime_details["hotwords_token_limit"] == 223
    assert backend.hotwords == "cloud, notes, Claude, Notepad, youtube, Calculator"
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    backend.close()
    backend.close()
    assert fake_whisper.state.unloaded == 1


@pytest.mark.parametrize("filename", REQUIRED_FILES)
@pytest.mark.parametrize("empty", [False, True])
def test_missing_assets_fail_before_library_can_download(fake_whisper, filename, empty):
    asset = fake_whisper.directory / filename
    if empty:
        asset.write_bytes(b"")
    else:
        asset.unlink()
    with pytest.raises(SttError, match="missing or empty"):
        fake_whisper.create()
    assert not fake_whisper.calls


@pytest.mark.parametrize("options", [{"name": "remote/model"}, {"device": "auto"}, {"device": "mps"}])
def test_unrecognized_family_or_device_rejected(fake_whisper, options):
    with pytest.raises(SttError):
        fake_whisper.create(**options)
    assert not fake_whisper.calls


@pytest.mark.parametrize("actual_device,actual_type,loaded", [
    ("cpu", "float16", True), ("cuda", "float32", True), ("cuda", "float16", False),
])
def test_cuda_fallback_or_unloaded_model_refused(fake_whisper, actual_device, actual_type, loaded):
    fake_whisper.state.actual_device = actual_device
    fake_whisper.state.actual_type = actual_type
    fake_whisper.state.loaded = loaded
    with pytest.raises(SttError, match="fallback refused"):
        fake_whisper.create(device="cuda")
    assert fake_whisper.state.unloaded == 1


def test_cpu_float_fallback_refused(fake_whisper):
    fake_whisper.state.actual_type = "float32"
    with pytest.raises(SttError, match="fallback refused"):
        fake_whisper.create()


@pytest.mark.parametrize("missing", ["device", "precision"])
def test_cuda_capability_checked_before_loading(fake_whisper, missing):
    if missing == "device":
        fake_whisper.state.cuda_count = 0
    else:
        fake_whisper.state.supported = {"float32"}
    with pytest.raises(SttError, match="fallback refused"):
        fake_whisper.create(device="cuda")
    assert not fake_whisper.calls


def test_lazy_decode_preserves_segments_and_final_event_timing(fake_whisper, monkeypatch):
    from voicebar.stt import whisper

    clock = [100.0]
    monkeypatch.setattr(whisper.time, "perf_counter", lambda: clock[0])
    backend = fake_whisper.create()
    events = []
    backend.start(events.append)
    audio = np.linspace(-0.3, 0.3, SAMPLE_RATE, dtype=np.float32)
    backend.feed(audio)
    clock[0] = 101.0
    backend.finish()
    assert [event.final for event in events] == [False, True]
    assert all(event.text == "Open Notepad." and event.backend == "whisper-large" for event in events)
    assert events[-1].emitted_at == 101.0
    assert events[-1].end_sample == len(audio)
    assert np.array_equal(fake_whisper.decoded[-1][0], audio)
    for _, options in fake_whisper.decoded:
        assert options == {"language": "en", "task": "transcribe", "temperature": 0.0,
                           "condition_on_previous_text": False, "beam_size": 5, "vad_filter": False,
                           "hotwords": backend.hotwords}
    assert backend.audio.size == 0
    assert backend.emit is None


def test_empty_final_is_emitted_and_can_be_reused(fake_whisper):
    backend = fake_whisper.create()
    fake_whisper.state.texts = []
    events = []
    for _ in range(2):
        backend.start(events.append)
        backend.feed(np.zeros(512, dtype=np.float32))
        backend.finish()
    assert len(events) == 2
    assert all(event.final and event.text == "" for event in events)


def test_silence_without_vad_speech_does_not_decode(fake_whisper):
    fake_whisper.state.probabilities = [0.0] * 100
    backend = fake_whisper.create()
    events = []
    backend.start(events.append)
    backend.feed(np.zeros(512 * 20, dtype=np.float32))
    backend.finish()
    assert events == []
    assert fake_whisper.decoded == []


def test_segment_generator_error_is_not_a_successful_truncated_final(fake_whisper):
    fake_whisper.state.failure = RuntimeError("native decode failed after first segment")
    backend = fake_whisper.create()
    events = []
    backend.start(events.append)
    backend.feed(np.zeros(512, dtype=np.float32))
    with pytest.raises(SttError, match="native decode failed"):
        backend.finish()
    assert not events
    assert backend.emit is None
    assert backend.audio.size == 0


def test_existing_endpoint_preserves_two_speech_segments_and_tail(fake_whisper):
    fake_whisper.state.probabilities = [0.0, 0.9, 0.9] + [0.0] * 12 + [0.9, 0.9]
    backend = fake_whisper.create(name="whisper-turbo")
    events = []
    backend.start(events.append)
    audio = np.zeros(512 * 17, dtype=np.float32)
    for begin in range(0, len(audio), 173):
        backend.feed(audio[begin:begin + 173])
    backend.finish()
    assert [event.line_id for event in events if event.final] == ["0", "1"]
    assert all(event.backend == "whisper-turbo" for event in events)


def test_windows_cuda_requires_local_cuda12_wheel(monkeypatch):
    from voicebar.stt import whisper

    monkeypatch.setattr(whisper.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda="13.0")))
    with pytest.raises(SttError, match="CUDA 12"):
        whisper._prepare_windows_cuda()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda="12.8")))
    whisper._prepare_windows_cuda()
