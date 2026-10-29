"""Parakeet utterance decoder with streaming Silero segmentation.

Partials are synchronous prefix redecodes on the recognition worker. There is
no queue of obsolete prefix jobs for a final to wait behind. Capture must live
on a separate thread; bounded audio backlog must fail rather than drop words.
"""

import json
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import numpy as np

from .base import SAMPLE_RATE, Backend, SttError, TextEvent, samples
from .models import disable_onnx_telemetry


class Silero:
    def __init__(self, directory):
        disable_onnx_telemetry()
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "silero_vad.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def probability(self, frame):
        if len(frame) != 512:
            raise SttError("Silero requires 512-sample frames.")
        value = np.concatenate((self.context, frame[None]), axis=1)
        result, self.state = self.session.run(
            ["output", "stateN"], {"input": value, "state": self.state, "sr": np.array([SAMPLE_RATE], dtype=np.int64)}
        )
        self.context = frame[None, -64:].copy()
        return float(result[0, 0])


class ParakeetBackend(Backend):
    name = "parakeet"
    partials_kind = "provisional-prefix-redecode"

    def __init__(self, directory, vad_directory, catalog, *, device="cpu", profile_directory=None):
        if device not in ("cpu", "cuda"):
            raise SttError(f"Unsupported Parakeet device: {device}. Use cpu or cuda.")
        disable_onnx_telemetry()
        import onnx_asr
        import onnxruntime as ort
        self.device = device
        self.sessions = {}
        self.profile_directory = Path(profile_directory).resolve() if profile_directory is not None else None
        if device == "cuda":
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise SttError("CUDA execution is unavailable. Use the separate GPU environment with "
                               "onnxruntime-gpu[cuda,cudnn]==1.31.0; do not install CPU ORT beside it.")
            # ORT 1.31's official NVIDIA package discovery supports CUDA 13 and
            # cuDNN 9. This loads local DLLs and does not download anything.
            ort.preload_dlls(cuda=True, cudnn=True, msvc=True, directory="")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        if self.profile_directory is not None:
            self.profile_directory.mkdir(parents=True, exist_ok=True)
            options.enable_profiling = True
            options.profile_file_prefix = str(self.profile_directory / "ort")
        kwargs = {"quantization": "int8", "providers": ["CPUExecutionProvider"], "sess_options": options}
        if device == "cuda":
            kwargs.update({
                "quantization": None,
                "providers": [("CUDAExecutionProvider", {
                    "device_id": 0,
                    "cudnn_conv_algo_search": "HEURISTIC",
                    "cudnn_conv_use_max_workspace": "0",
                }), "CPUExecutionProvider"],
                # Match the CPU feature pipeline; the encoder and decoder run
                # on CUDA, while NumPy feature extraction and Silero stay CPU.
                "preprocessor_config": {"use_numpy_preprocessors": True},
            })
        self.model = onnx_asr.load_model(
            "nemo-parakeet-tdt-0.6b-v3", directory, **kwargs,
        )
        if device == "cuda" or self.profile_directory is not None:
            # These two session fields are checked against the pinned 0.12.0
            # adapter. Fail explicitly if a dependency update changes them.
            if version("onnx-asr") != "0.12.0":
                raise SttError("CUDA placement verification requires the pinned onnx-asr 0.12.0 adapter.")
            adapter = self.model.asr
            self.sessions = {"encoder": adapter._encoder, "decoder_joint": adapter._decoder_joint}
            if device == "cuda":
                for name, session in self.sessions.items():
                    if session.get_providers()[0] != "CUDAExecutionProvider":
                        self.model = None
                        self.sessions = {}
                        raise SttError(f"Parakeet {name} did not initialize CUDA; CPU fallback refused. "
                                       "Check the GPU environment's local CUDA/cuDNN DLLs.")
        self.vad = Silero(vad_directory)
        self.emit = None

    def collect_profiles(self):
        """Stop profiling and report actual per-node execution for this decode.

        Calling get_providers() checks registration only. A CUDA smoke must
        additionally find real compute nodes on CUDA in both ASR sessions.
        """
        if self.profile_directory is None:
            raise SttError("Construct Parakeet with profile_directory to collect placement evidence.")
        report = {}
        for name, session in self.sessions.items():
            generated = Path(session.end_profiling())
            payload = generated.read_text(encoding="utf-8")
            # ORT timestamps may give multiple sessions the same filename. Read
            # and preserve each session before ending the next one's profile.
            destination = self.profile_directory / f"{name}.json"
            destination.write_text(payload, encoding="utf-8")
            report[name] = summarize_profile(json.loads(payload)) | {
                "profile": str(destination), "registered_providers": session.get_providers(),
                "provider_options": session.get_provider_options(),
            }
        return report

    def start(self, emit):
        if self.emit is not None:
            raise SttError("Finish the previous Parakeet utterance first.")
        self.emit = emit
        self.audio = np.empty(0, dtype=np.float32)
        self.processed = 0
        self.active_start = None
        self.last_speech = 0
        self.next_partial = 0
        self.line = 0
        self.last_text = ""
        self._completed_segment = False
        self.vad.reset()

    def transcribe_segment(self, audio, emit):
        """Keep every streaming VAD/final boundary; omit provisional decoding."""
        self.start(emit)
        self._completed_segment = True
        try:
            self.feed(audio)
            self.finish()
        except BaseException:
            self.close()
            raise
        finally:
            self._completed_segment = False

    def _decode(self, end, final):
        if self.active_start is None:
            return
        began = time.perf_counter()
        text = self.model.recognize(self.audio[self.active_start:end], sample_rate=SAMPLE_RATE).strip()
        cost = time.perf_counter() - began
        if text != self.last_text or final:
            self.emit(TextEvent(str(self.line), text, final, self.active_start, end,
                                time.perf_counter(), self.name, self.device))
            self.last_text = text
        self.next_partial = self.processed + int(max(0.8, cost) * SAMPLE_RATE)

    def feed(self, audio):
        if self.emit is None:
            raise SttError("Parakeet stream has not started.")
        self.audio = np.concatenate((self.audio, samples(audio)))
        if len(self.audio) > SAMPLE_RATE * 120:
            raise SttError("Utterance exceeds the 120-second recording limit.")
        while self.processed + 512 <= len(self.audio):
            offset = self.processed
            probability = self.vad.probability(self.audio[offset:offset + 512])
            self.processed += 512
            if self.active_start is None and probability >= 0.5:
                self.active_start = max(0, offset - int(0.2 * SAMPLE_RATE))
                self.last_text = ""
                self.next_partial = self.processed + int(0.8 * SAMPLE_RATE)
            if self.active_start is not None:
                if probability >= 0.35:
                    self.last_speech = self.processed
                # Endpoint wait is part of measured speech-to-final latency.
                if self.processed - self.last_speech >= int(0.3 * SAMPLE_RATE):
                    self._decode(min(self.processed, self.last_speech + int(0.15 * SAMPLE_RATE)), True)
                    self.active_start = None
                    self.line += 1
                elif not self._completed_segment and self.processed >= self.next_partial:
                    self._decode(self.processed, False)

    def finish(self):
        if self.emit is None:
            raise SttError("Parakeet stream has not started.")
        try:
            if self.processed < len(self.audio):
                tail = self.audio[self.processed:]
                probability = self.vad.probability(np.pad(tail, (0, 512 - len(tail))))
                if self.active_start is None and probability >= 0.5:
                    self.active_start = max(0, self.processed - int(0.2 * SAMPLE_RATE))
            self._decode(len(self.audio), True)
        finally:
            self.emit = None
            self.audio = np.empty(0, dtype=np.float32)
            self.vad.reset()

    def close(self):
        self.emit = None
        self.audio = np.empty(0, dtype=np.float32)
        self.model = None
        self.vad = None
        self.sessions = {}


def summarize_profile(events):
    """Count executed kernel events, excluding copies from CUDA compute proof."""
    if not isinstance(events, list):
        raise SttError("ORT placement profile must be an event list.")
    providers = Counter()
    cuda_operations = Counter()
    cpu_operations = Counter()
    for event in events:
        if not isinstance(event, dict) or event.get("cat") != "Node":
            continue
        args = event.get("args", {})
        provider = args.get("provider")
        operation = args.get("op_name", "")
        if not provider:
            continue
        providers[provider] += 1
        if operation and not operation.startswith("Memcpy"):
            if provider == "CUDAExecutionProvider":
                cuda_operations[operation] += 1
            elif provider == "CPUExecutionProvider":
                cpu_operations[operation] += 1
    return {"executed_provider_nodes": dict(providers),
            "cuda_compute_nodes": sum(cuda_operations.values()), "cuda_operations": dict(cuda_operations),
            "cpu_compute_nodes": sum(cpu_operations.values()), "cpu_operations": dict(cpu_operations)}
