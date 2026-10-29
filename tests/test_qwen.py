"""Qwen adapter contract with fake tensors; no models, audio devices or network."""

import hashlib
import sys
import weakref
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.stt.base import SAMPLE_RATE, SttError, transcribe_segment
from voicebar.stt.qwen import QwenBackend


class Tensor:
    def __init__(self, values, *, device="cpu", dtype="int64"):
        self.values = np.asarray(values)
        self.device = device
        self.dtype = dtype

    @property
    def shape(self):
        return self.values.shape

    @property
    def ndim(self):
        return self.values.ndim

    def is_floating_point(self):
        return self.dtype in ("float32", "bfloat16")

    def __getitem__(self, key):
        value = self.values[key]
        if np.isscalar(value):
            return value
        return Tensor(value, device=self.device, dtype=self.dtype)

    def detach(self):
        return self

    def cpu(self):
        return Tensor(self.values, dtype=self.dtype)


class Batch(dict):
    fallback = False

    def to(self, device, dtype):
        for value in self.values():
            value.device = "cpu" if self.fallback else device
            if value.is_floating_point():
                value.dtype = dtype
        return self


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    from voicebar.stt import qwen

    state = SimpleNamespace(
        calls=[], requests=[], generations=[], decoded_tokens=[], text="open Claude",
        model_fallback=False, input_fallback=False, output_fallback=False,
        cuda_available=True, bf16_supported=True, decode_error=None,
        token_count=3, eos=True, models=[], threads=16, thread_settings=[],
        load_threads=[], thread_setting_ignored=False, tokenization_calls=[],
        input_refs=[], output_refs=[], cache_releases=0, cache_release_error=None,
    )

    class Processor:
        def __init__(self):
            self.tokenizer = SimpleNamespace(encode=self.encode)

        @staticmethod
        def encode(text, **kwargs):
            state.tokenization_calls.append((text, kwargs))
            return list(range(len(text.split())))

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            state.calls.append(("processor", path, kwargs))
            state.load_threads.append(state.threads)
            return cls()

        def apply_transcription_request(self, **kwargs):
            state.requests.append(kwargs)
            batch = Batch({
                "input_ids": Tensor([[10, 11]]),
                "input_features": Tensor([[0.1, 0.2]], dtype="float32"),
                "input_features_mask": Tensor([[1, 1]]),
            })
            batch.fallback = state.input_fallback
            state.input_refs.extend(weakref.ref(value) for value in batch.values())
            return batch

        def decode(self, tokens, **kwargs):
            assert kwargs == {"return_format": "transcription_only"}
            assert tokens.device == "cpu"
            state.decoded_tokens.append(tokens.values.tolist())
            return [state.text]

    class Model:
        generation_config = SimpleNamespace(eos_token_id=99)

        def __init__(self, dtype):
            self.parameter = Tensor([0.1], dtype=dtype)
            self.buffer = Tensor([0.1], dtype=dtype)

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            state.calls.append(("model", path, kwargs))
            state.load_threads.append(state.threads)
            model = cls(kwargs["dtype"])
            state.models.append(model)
            return model

        def to(self, device):
            if not state.model_fallback:
                self.parameter.device = device
                self.buffer.device = device
            return self

        def eval(self):
            return self

        def parameters(self):
            return iter([self.parameter])

        def buffers(self):
            return iter([self.buffer])

        def generate(self, **kwargs):
            state.generations.append(kwargs)
            if state.decode_error:
                raise state.decode_error
            tokens = [20] * state.token_count
            if state.eos and tokens:
                tokens[-1] = 99
            device = "cpu" if state.output_fallback else self.parameter.device
            output = Tensor([[10, 11, *tokens]], device=device)
            state.output_refs.append(weakref.ref(output))
            return output

    def set_num_threads(count):
        state.thread_settings.append(count)
        if not state.thread_setting_ignored:
            state.threads = count

    def empty_cache():
        state.cache_releases += 1
        if state.cache_release_error:
            raise state.cache_release_error

    torch = SimpleNamespace(
        device=lambda value: value, float32="float32", bfloat16="bfloat16",
        cuda=SimpleNamespace(is_available=lambda: state.cuda_available,
                             is_bf16_supported=lambda: state.bf16_supported,
                             empty_cache=empty_cache),
        inference_mode=nullcontext, is_tensor=lambda value: isinstance(value, Tensor),
        set_num_threads=set_num_threads, get_num_threads=lambda: state.threads,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoProcessor=Processor, AutoModelForMultimodalLM=Model,
    ))
    monkeypatch.setattr(qwen, "Silero", lambda directory: SimpleNamespace(
        reset=lambda: None,
        probability=lambda frame: 0.9 if np.any(frame) else 0.0,
    ))
    state.directory = tmp_path / "qwen"
    state.directory.mkdir()
    state.catalog = SimpleNamespace(apps=["Claude", "Autodesk Fusion", "A, B"],
                                    aliases={"cloud": "Claude"}, sites={"youtube": "url"},
                                    engines={"google": "https://google.com/?q={query}"})
    state.make = lambda **kwargs: QwenBackend(state.directory, tmp_path / "silero", state.catalog, **kwargs)
    return state


@pytest.mark.parametrize("device,dtype", [("cpu", "float32"), ("cuda", "bfloat16")])
def test_local_load_exact_device_and_dtype(fakes, device, dtype, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    backend = fakes.make(device=device)
    assert backend.device == device
    assert backend.dtype == dtype
    assert backend.placement["parameter_devices"] == ["cuda:0" if device == "cuda" else "cpu"]
    assert backend.placement["generation_batch_size"] == 1
    assert [call[0] for call in fakes.calls] == ["processor", "model"]
    for _, path, kwargs in fakes.calls:
        assert path == str(fakes.directory.resolve())
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
    assert fakes.calls[-1][2]["attn_implementation"] == "sdpa"
    assert "device_map" not in fakes.calls[-1][2]
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_prompt_is_fixed_grammar_and_catalog_context_with_evidence(fakes):
    from voicebar.stt.context import CONTEXT_PROFILE, DOMAIN_CONTEXT

    backend = fakes.make()
    assert backend.prompt.startswith(DOMAIN_CONTEXT + "\nVocabulary: ")
    terms = backend.prompt.split("\nVocabulary: ", 1)[1].splitlines()[0].removesuffix(".").split(", ")
    assert {"Claude", "Autodesk Fusion", "cloud", "youtube", "google", "open", "write", "pause"} <= set(terms)
    assert "A, B" not in terms
    assert backend.placement["context_profile"] == CONTEXT_PROFILE
    assert backend.placement["context_sha256"] == hashlib.sha256(backend.prompt.encode()).hexdigest()
    assert backend.placement["context_token_count"] == len(backend.prompt.split())
    assert fakes.tokenization_calls == [(backend.prompt, {"add_special_tokens": False})]


def test_cpu_thread_bound_is_applied_before_model_loading(fakes):
    backend = fakes.make()
    assert fakes.thread_settings == [4]
    assert fakes.load_threads == [4, 4]
    assert backend.placement["torch_cpu_threads"] == 4


def test_cuda_reports_actual_host_threads_without_changing_them(fakes):
    backend = fakes.make(device="cuda")
    assert fakes.thread_settings == []
    assert fakes.load_threads == [16, 16]
    assert backend.placement["torch_cpu_threads"] == 16


def test_cpu_thread_setting_failure_refuses_model_load(fakes):
    fakes.thread_setting_ignored = True
    with pytest.raises(SttError, match="could not establish.*CPU bound"):
        fakes.make()
    assert fakes.calls == []


def test_decode_reports_observed_cpu_threads(fakes):
    backend = fakes.make()
    fakes.threads = 2
    backend.start(lambda event: None)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    backend.finish()
    assert backend.placement["torch_cpu_threads"] == 4
    assert backend.placement["last_torch_cpu_threads"] == 2


def test_cpu_thread_bound_changed_after_load_refuses_decode(fakes):
    backend = fakes.make()
    fakes.threads = 16
    backend.start(lambda event: None)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    with pytest.raises(SttError, match="CPU bound changed"):
        backend.finish()
    assert fakes.requests == []
    assert backend.emit is None and backend.audio.size == 0


def test_language_sample_rate_slicing_and_deterministic_generate(fakes):
    backend = fakes.make(device="cuda")
    events = []
    audio = np.full(1000, 0.2, dtype=np.float32)
    backend.start(events.append)
    backend.feed(audio)
    backend.finish()
    assert len(events) == 1 and events[0].final
    assert events[0].text == "open Claude" and events[0].backend == "qwen"
    assert events[0].device == "cuda" and events[0].end_sample == len(audio)
    request = fakes.requests[0]
    assert request["language"] == "English"
    assert request["processor_kwargs"] == {"audio_kwargs": {"sampling_rate": SAMPLE_RATE}}
    np.testing.assert_array_equal(request["audio"], audio)
    assert fakes.decoded_tokens == [[[20, 20, 99]]]
    generation = fakes.generations[0]
    assert {key: generation[key] for key in ("max_new_tokens", "do_sample", "num_beams", "use_cache")} == {
        "max_new_tokens": 256, "do_sample": False, "num_beams": 1, "use_cache": True,
    }
    assert backend.placement["last_output_device"] == "cuda:0"
    assert set(backend.placement["last_input_devices"].values()) == {"cuda:0"}


def test_shared_vad_prefix_partial_endpoint_and_every_sample(fakes):
    backend = fakes.make()
    events = []
    backend.start(events.append)
    audio = np.concatenate((np.zeros(512 * 2), np.full(512 * 29, 0.2), np.zeros(512 * 12))).astype(np.float32)
    for begin in range(0, len(audio), 173):
        backend.feed(audio[begin:begin + 173])
    backend.finish()
    assert [event.final for event in events] == [False, True]
    final = events[-1]
    assert final.start_sample == 0  # unchanged 200 ms prepad
    assert final.end_sample == 512 * 31 + int(0.15 * SAMPLE_RATE)
    np.testing.assert_array_equal(fakes.requests[-1]["audio"], audio[:final.end_sample])
    assert backend.emit is None and backend.audio.size == 0


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_completed_long_segment_omits_provisional_generation_and_preserves_streaming_finals(fakes, device):
    backend = fakes.make(device=device)
    audio = np.concatenate((np.zeros(512 * 2), np.full(512 * 311, 0.2),
                            np.zeros(512 * 12 + 173))).astype(np.float32)
    frames, completed = [], []
    probability = backend.vad.probability

    def observe(frame):
        frames.append(frame.copy())
        return probability(frame)

    backend.vad.probability = observe
    transcribe_segment(backend, audio, completed.append)
    assert len(fakes.generations) == 1, "Completed audio generated a provisional prefix"
    assert len(completed) == 1 and completed[0].final
    assert completed[0].end_sample == 512 * 313 + int(0.15 * SAMPLE_RATE)
    completed_request = fakes.requests[0]
    np.testing.assert_array_equal(completed_request["audio"], audio[:completed[0].end_sample])
    np.testing.assert_array_equal(np.concatenate(frames)[:len(audio)], audio)
    assert len(frames) == (len(audio) + 511) // 512
    assert not backend._completed_segment and backend.emit is None and not backend.audio.size

    completed_frames = [frame.copy() for frame in frames]
    frames.clear()
    streaming, generation_count = [], len(fakes.generations)
    backend.start(streaming.append)
    for begin in range(0, len(audio), 173):
        backend.feed(audio[begin:begin + 173])
    backend.finish()
    assert len(fakes.generations) > generation_count + 1
    assert any(not event.final for event in streaming), "Streaming partials were disabled"
    final = [event for event in streaming if event.final]
    assert [(e.text, e.start_sample, e.end_sample, e.device) for e in final] == [
        (e.text, e.start_sample, e.end_sample, e.device) for e in completed
    ]
    assert len(frames) == len(completed_frames)
    for actual, expected in zip(frames, completed_frames, strict=True):
        np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(fakes.requests[-1]["audio"], completed_request["audio"])
    assert fakes.requests[-1]["prompt"] == completed_request["prompt"] == backend.prompt
    assert fakes.requests[-1]["language"] == completed_request["language"] == "English"
    assert fakes.requests[-1]["processor_kwargs"] == completed_request["processor_kwargs"]


def test_completed_segment_preserves_multiple_genuine_endpoints_and_speech_tail(fakes):
    backend = fakes.make()
    audio = np.concatenate((np.full(512 * 60, 0.2), np.zeros(512 * 12),
                            np.full(512 * 50 + 173, 0.3))).astype(np.float32)
    events = []
    transcribe_segment(backend, audio, events.append)
    assert [(event.line_id, event.final) for event in events] == [("0", True), ("1", True)]
    assert len(fakes.generations) == 2
    assert events[0].end_sample == 512 * 60 + int(0.15 * SAMPLE_RATE)
    assert events[-1].end_sample == len(audio)
    for request, event in zip(fakes.requests, events, strict=True):
        np.testing.assert_array_equal(request["audio"], audio[event.start_sample:event.end_sample])


def test_completed_silence_still_emits_empty_final_without_generation(fakes):
    backend, events = fakes.make(), []
    transcribe_segment(backend, np.zeros(777, dtype=np.float32), events.append)
    assert [(event.text, event.final, event.end_sample) for event in events] == [("", True, 777)]
    assert fakes.requests == fakes.generations == []
    assert not backend._completed_segment and backend.emit is None and not backend.audio.size


def test_explicit_startup_warmup_discards_generated_text_and_retains_model(fakes):
    backend = fakes.make(device="cuda")
    backend.warmup()
    assert len(fakes.generations) == 1
    assert np.count_nonzero(fakes.requests[0]["audio"]) == 0
    assert len(fakes.requests[0]["audio"]) == SAMPLE_RATE
    assert backend.model is not None
    assert fakes.cache_releases == 1
    assert backend.emit is None and not backend.audio.size and backend.active_start is None
    events = []
    transcribe_segment(backend, np.zeros(777, dtype=np.float32), events.append)
    assert len(fakes.generations) == 1  # ordinary silent input still never decodes
    assert [event.text for event in events] == [""]
    assert fakes.cache_releases == 1  # silent input did not allocate utterance tensors


def test_final_cuda_cache_release_drops_utterance_tensors_before_callback(fakes, monkeypatch):
    backend, events, released = fakes.make(device="cuda"), [], []

    def verify_release():
        # The fake generator records its arguments; remove that test-only owner
        # so these weak references reveal references retained by the adapter.
        fakes.generations.clear()
        assert fakes.input_refs and fakes.output_refs
        assert all(reference() is None for reference in fakes.input_refs + fakes.output_refs)
        assert backend.model is fakes.models[0]
        assert backend.model.parameter.device == "cuda:0"
        assert not events, "Final text was published before memory was released"
        released.append(True)

    monkeypatch.setattr(backend.torch.cuda, "empty_cache", verify_release)
    transcribe_segment(backend, np.full(1000, 0.2, dtype=np.float32), events.append)
    assert released == [True]
    assert [(event.text, event.final) for event in events] == [("open Claude", True)]


def test_cuda_partials_retain_cache_until_completed_decode(fakes):
    backend, events = fakes.make(device="cuda"), []
    backend.start(events.append)
    backend.feed(np.full(SAMPLE_RATE * 2, 0.2, dtype=np.float32))
    assert events and all(not event.final for event in events)
    assert fakes.cache_releases == 0
    backend.finish()
    assert events[-1].final
    assert fakes.cache_releases == 1


def test_cpu_warmup_and_completed_decode_never_release_cuda_cache(fakes):
    backend, events = fakes.make(device="cpu"), []
    backend.warmup()
    transcribe_segment(backend, np.full(1000, 0.2, dtype=np.float32), events.append)
    assert len(fakes.generations) == 2 and len(events) == 1
    assert fakes.cache_releases == 0


def test_cuda_cache_release_failure_accepts_no_transcript(fakes):
    backend, events = fakes.make(device="cuda"), []
    fakes.cache_release_error = RuntimeError("cache release failed")
    with pytest.raises(SttError, match="cache release failed"):
        transcribe_segment(backend, np.full(1000, 0.2, dtype=np.float32), events.append)
    assert not events
    assert backend.emit is None and not backend.audio.size


def test_explicit_warmup_refuses_an_active_utterance(fakes):
    backend = fakes.make()
    backend.start(lambda event: None)
    with pytest.raises(SttError, match="recording is in progress"):
        backend.warmup()
    assert fakes.generations == []


def test_warmup_failure_clears_temporary_audio_without_a_transcript_callback(fakes):
    backend = fakes.make()
    fakes.decode_error = RuntimeError("generation failure")
    with pytest.raises(SttError, match="generation failure"):
        backend.warmup()
    assert backend.emit is None and not backend.audio.size and backend.active_start is None


def test_completed_segment_still_refuses_incomplete_token_limited_final(fakes):
    fakes.token_count, fakes.eos = 256, False
    backend, events = fakes.make(), []
    with pytest.raises(SttError, match="token limit"):
        transcribe_segment(backend, np.full(512 * 311, 0.2, dtype=np.float32), events.append)
    assert len(fakes.generations) == 1 and not events
    assert not backend._completed_segment and backend.emit is None and not backend.audio.size
    assert backend.model is None


def test_silence_has_empty_final_without_model_generation(fakes):
    backend = fakes.make()
    events = []
    backend.start(events.append)
    backend.feed(np.zeros(777, dtype=np.float32))
    backend.finish()
    assert [(event.text, event.final, event.end_sample) for event in events] == [("", True, 777)]
    assert fakes.requests == fakes.generations == []


def test_empty_decoded_transcript_is_final_not_dropped(fakes):
    fakes.text = ""
    backend = fakes.make()
    events = []
    backend.start(events.append)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    backend.finish()
    assert len(events) == 1 and events[0].text == "" and events[0].final


@pytest.mark.parametrize("setting,message", [
    ("cuda_available", "CUDA execution is unavailable"),
    ("bf16_supported", "bfloat16 support"),
])
def test_cuda_unavailable_never_falls_back(fakes, setting, message):
    setattr(fakes, setting, False)
    with pytest.raises(SttError, match=message):
        fakes.make(device="cuda")
    assert fakes.calls == []


def test_model_silent_cpu_fallback_is_rejected(fakes):
    fakes.model_fallback = True
    with pytest.raises(SttError, match="silent device fallback"):
        fakes.make(device="cuda")


@pytest.mark.parametrize("setting,message", [
    ("input_fallback", "inputs were not placed"),
    ("output_fallback", "output placement"),
])
def test_inference_device_fallback_is_rejected(fakes, setting, message):
    backend = fakes.make(device="cuda")
    setattr(fakes, setting, True)
    events = []
    backend.start(events.append)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    with pytest.raises(SttError, match=message):
        backend.finish()
    assert events == [] and backend.emit is None and backend.audio.size == 0


def test_truncation_refuses_incomplete_final(fakes):
    fakes.token_count, fakes.eos = 256, False
    backend = fakes.make()
    events = []
    backend.start(events.append)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    with pytest.raises(SttError, match="token limit"):
        backend.finish()
    assert events == []


def test_exact_token_limit_with_eos_is_valid(fakes):
    fakes.token_count = 256
    backend = fakes.make()
    events = []
    backend.start(events.append)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    backend.finish()
    assert len(events) == 1 and events[0].final


def test_decoder_error_clears_finish_and_accepts_no_transcript(fakes):
    fakes.decode_error = RuntimeError("out of memory")
    backend = fakes.make()
    events = []
    backend.start(events.append)
    backend.feed(np.full(100, 0.2, dtype=np.float32))
    with pytest.raises(SttError, match="out of memory"):
        backend.finish()
    assert events == [] and backend.emit is None and backend.audio.size == 0


def test_closed_and_invalid_lifecycle(fakes):
    backend = fakes.make()
    with pytest.raises(SttError, match="not started"):
        backend.finish()
    backend.start(lambda event: None)
    with pytest.raises(SttError, match="previous"):
        backend.start(lambda event: None)
    backend.close()
    assert backend.model is None and backend.processor is None and backend.vad is None
    with pytest.raises(SttError, match="closed"):
        backend.start(lambda event: None)


def test_invalid_device_or_missing_local_directory(fakes, tmp_path):
    with pytest.raises(SttError, match="Unsupported Qwen device"):
        fakes.make(device="auto")
    with pytest.raises(SttError, match="existing local model directory"):
        QwenBackend(tmp_path / "absent", tmp_path, fakes.catalog)
    assert fakes.calls == []
