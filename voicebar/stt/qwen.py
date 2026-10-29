"""Local native-Transformers Qwen3-ASR with the existing Silero endpoint.

The official Qwen model card documents ``apply_transcription_request`` and
``AutoModelForMultimodalLM`` in Transformers >= 5.13.0. Vocabulary is drawn
from live command grammar and the catalog; expected transcripts never enter it.
Partials are provisional prefix redecodes, not native streaming inference.
"""

import hashlib
import time
from pathlib import Path

import numpy as np

from .base import SAMPLE_RATE, SttError, TextEvent
from .context import CONTEXT_PROFILE, build_context
from .models import offline_environment
from .parakeet import ParakeetBackend, Silero


class QwenBackend(ParakeetBackend):
    """Reuse Parakeet's exact segmentation; replace only utterance decoding."""

    name = "qwen"
    hotwords_supported = True
    partials_kind = "provisional-prefix-redecode"
    max_new_tokens = 256

    def __init__(self, directory, vad_directory, catalog, *, device="cpu"):
        if device not in ("cpu", "cuda"):
            raise SttError(f"Unsupported Qwen device: {device}. Use cpu or cuda.")
        directory = Path(directory).resolve()
        if not directory.is_dir():
            raise SttError("Qwen requires an existing local model directory. Run scripts/setup.py.")
        offline_environment()
        try:
            import torch
            from transformers import AutoModelForMultimodalLM, AutoProcessor
        except (ImportError, OSError) as error:
            raise SttError("Qwen needs the separate local Torch environment with native "
                           "Transformers >= 5.13.0; no runtime installation is attempted.") from error
        self.device = device
        self.torch = torch
        self.tensor_device = torch.device("cuda:0" if device == "cuda" else "cpu")
        self.dtype = torch.bfloat16 if device == "cuda" else torch.float32
        if device == "cuda" and not torch.cuda.is_available():
            raise SttError("Qwen CUDA execution is unavailable; CPU fallback refused.")
        if device == "cuda" and not torch.cuda.is_bf16_supported():
            raise SttError("Qwen CUDA requires bfloat16 support; CPU fallback refused.")
        self.prompt = build_context(catalog)
        self.context_sha256 = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        self.emit = None
        self.model = None
        self.processor = None
        self.sessions = {}
        self.placement = {}
        try:
            # Bound CPU inference before loading or running the model, matching
            # the other CPU candidates. CUDA's host thread setting is observed
            # in evidence without changing the process-wide Torch setting.
            if device == "cpu":
                torch.set_num_threads(4)
                if torch.get_num_threads() != 4:
                    raise SttError("Qwen could not establish the four-thread CPU bound.")
            # Absolute local paths, no remote code, no auto placement/offload.
            self.processor = AutoProcessor.from_pretrained(
                str(directory), local_files_only=True, trust_remote_code=False,
            )
            self.context_token_count = len(self.processor.tokenizer.encode(self.prompt, add_special_tokens=False))
            self.model = AutoModelForMultimodalLM.from_pretrained(
                str(directory), local_files_only=True, trust_remote_code=False,
                dtype=self.dtype, attn_implementation="sdpa",
            ).to(self.tensor_device).eval()
            self._verify_model_placement()
            self.vad = Silero(Path(vad_directory))
        except SttError:
            self.model = None
            self.processor = None
            raise
        except Exception as error:
            self.model = None
            self.processor = None
            raise SttError(f"Local Qwen model could not initialize: {error}") from error

    def _verify_model_placement(self):
        parameters = list(self.model.parameters())
        if not parameters:
            raise SttError("Qwen model has no parameters; device placement cannot be verified.")
        wrong = [str(value.device) for value in parameters if value.device != self.tensor_device]
        wrong += [str(value.device) for value in self.model.buffers() if value.device != self.tensor_device]
        if wrong:
            raise SttError(f"Qwen did not place every model tensor on {self.tensor_device}; "
                           "silent device fallback/offload refused.")
        if any(value.is_floating_point() and value.dtype != self.dtype for value in parameters):
            raise SttError("Qwen model dtype differs from the requested local execution dtype.")
        self.placement = {
            "requested_device": self.device,
            "parameter_devices": sorted({str(value.device) for value in parameters}),
            "parameter_dtypes": sorted({str(value.dtype) for value in parameters}),
            "attention_implementation": "sdpa", "generation_batch_size": 1,
            "torch_cpu_threads": self.torch.get_num_threads(),
            "context_profile": CONTEXT_PROFILE, "context_sha256": self.context_sha256,
            "context_token_count": self.context_token_count,
            "context_token_scope": "prompt text only; excludes chat template, audio tokens and language prefill",
        }

    def start(self, emit):
        if self.model is None or self.processor is None or self.vad is None:
            raise SttError("Qwen backend is closed.")
        super().start(emit)
        self.had_final = False

    def warmup(self):
        """Initialize local feature/generation kernels before capture is ready.

        This is an explicit startup operation, separate from recognizing input:
        a fixed zero buffer never reaches the gate, a callback, or execution.
        The model stays resident; every temporary utterance buffer is cleared.
        Ordinary silent recordings still follow VAD and never invoke decoding.
        """
        if self.emit is not None:
            raise SttError("Cannot warm Qwen while a recording is in progress.")
        self.start(lambda event: None)
        try:
            self.audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
            self.active_start = 0
            self.processed = len(self.audio)
            self._decode(len(self.audio), True)
            self.placement["startup_warmup"] = "one-second local zero PCM; transcript discarded"
        finally:
            self.emit = None
            self.audio = np.empty(0, dtype=np.float32)
            self.active_start = None
            self.vad.reset()

    def _decode(self, end, final):
        if self.active_start is None:
            return
        began = time.perf_counter()
        try:
            threads = self.torch.get_num_threads()
            if self.device == "cpu" and threads > 4:
                raise SttError("Qwen's four-thread CPU bound changed; recognition refused.")
            with self.torch.inference_mode():
                inputs = self.processor.apply_transcription_request(
                    audio=self.audio[self.active_start:end], language="English",
                    prompt=self.prompt,
                    processor_kwargs={"audio_kwargs": {"sampling_rate": SAMPLE_RATE}},
                ).to(self.tensor_device, self.dtype)
                tensors = {name: value for name, value in inputs.items() if self.torch.is_tensor(value)}
                if not tensors or any(value.device != self.tensor_device for value in tensors.values()):
                    raise SttError("Qwen inputs were not placed on the requested device; fallback refused.")
                if inputs["input_ids"].shape[0] != 1:
                    raise SttError("Qwen recognition requires a batch of exactly one recording.")
                output = self.model.generate(
                    **inputs, max_new_tokens=self.max_new_tokens,
                    do_sample=False, num_beams=1, use_cache=True,
                )
                if output.device != self.tensor_device or output.ndim != 2 or output.shape[0] != 1:
                    raise SttError("Qwen output placement or batch size is invalid; fallback refused.")
                generated = output[:, inputs["input_ids"].shape[1]:].detach().cpu()
                eos = self.model.generation_config.eos_token_id
                eos_ids = {eos} if isinstance(eos, int) else set(eos or [])
                if generated.shape[1] >= self.max_new_tokens and (
                    not generated.shape[1] or int(generated[0, -1]) not in eos_ids
                ):
                    raise SttError("Qwen hit its transcript token limit; incomplete text discarded.")
                decoded = self.processor.decode(generated, return_format="transcription_only")
                if isinstance(decoded, list) and len(decoded) == 1:
                    decoded = decoded[0]
                if not isinstance(decoded, str):
                    raise SttError("Qwen returned an invalid transcription.")
                text = decoded.strip()
                self.placement.update({
                    "last_input_devices": {name: str(value.device) for name, value in tensors.items()},
                    "last_output_device": str(output.device),
                    "last_generated_tokens": generated.shape[1],
                    "last_torch_cpu_threads": threads,
                })
            if final and self.device == "cuda":
                # Drop utterance tensors before returning idle allocator memory
                # to other local GPU models. Warmup uses this same final path;
                # provisional decodes retain their cache for the next prefix.
                del inputs, tensors, output, generated
                self.torch.cuda.empty_cache()
        except SttError:
            raise
        except Exception as error:
            raise SttError(f"Qwen recognition failed; no transcript accepted: {error}") from error
        cost = time.perf_counter() - began
        if text != self.last_text or final:
            self.emit(TextEvent(str(self.line), text, final, self.active_start, end,
                                time.perf_counter(), self.name, self.device))
            self.last_text = text
        self.had_final = self.had_final or final
        # The shared feed loop consumes every sample. Expensive provisional
        # work postpones its next prefix; there is no obsolete inference queue.
        self.next_partial = self.processed + int(max(0.8, cost) * SAMPLE_RATE)

    def finish(self):
        if self.emit is None:
            raise SttError("Qwen stream has not started.")
        emit, end = self.emit, len(self.audio)
        super().finish()
        # Silence still produces an explicit empty final; it never invents
        # audio, bypasses VAD, or asks the decoder to hallucinate from silence.
        if not self.had_final:
            emit(TextEvent(str(self.line), "", True, 0, end, time.perf_counter(), self.name, self.device))

    def close(self):
        self.emit = None
        self.audio = np.empty(0, dtype=np.float32)
        self.model = None
        self.processor = None
        self.vad = None
        self.sessions = {}
