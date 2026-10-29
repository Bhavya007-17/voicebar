"""Local faster-whisper decoders with the existing Silero streaming boundary.

Whisper has no native streaming partials. Prefix redecodes, VAD thresholds,
pre-roll, endpoint wait, and flush behavior are inherited from Parakeet so the
candidate comparison does not tune segmentation against the recordings.
"""

import os
import time
from pathlib import Path

from .base import SAMPLE_RATE, SttError, TextEvent, keyterms
from .models import offline_environment
from .parakeet import ParakeetBackend, Silero

REQUIRED_FILES = (
    "config.json", "model.bin", "tokenizer.json",
    "preprocessor_config.json", "vocabulary.json",
)


def _prepare_windows_cuda():
    """Preload CUDA 12/cuDNN DLLs from the isolated local PyTorch wheel.

    PyTorch's Windows import loads its bundled DLLs. CTranslate2's documented
    Windows GPU wheel needs CUDA 12 and cuDNN 9; it must not discover the CUDA
    13 libraries used by the separate Parakeet environment instead.
    """
    if os.name != "nt":
        return
    try:
        import torch
    except (ImportError, OSError) as error:
        raise SttError("Whisper CUDA on Windows requires the isolated CUDA 12 PyTorch environment "
                       "to preload its local cuBLAS/cuDNN DLLs.") from error
    if not str(torch.version.cuda or "").startswith("12."):
        raise SttError("Whisper CUDA requires the isolated CUDA 12 PyTorch wheel on Windows.")


class WhisperBackend(ParakeetBackend):
    """Whisper large-v3 or Turbo, CPU INT8 or CUDA FP16 with no CPU fallback."""

    name = "whisper-large"
    hotwords_supported = True
    partials_kind = "provisional-prefix-redecode"

    def __init__(self, directory, vad_directory, catalog, *, device="cpu", name="whisper-large"):
        if name not in ("whisper-large", "whisper-turbo"):
            raise SttError(f"Unsupported Whisper model family: {name}.")
        if device not in ("cpu", "cuda"):
            raise SttError(f"Unsupported Whisper device: {device}. Use cpu or cuda.")
        directory = Path(directory).resolve()
        missing = [name for name in REQUIRED_FILES
                   if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
        if missing:
            # In particular, absent tokenizer.json would make faster-whisper
            # call Tokenizer.from_pretrained even with a local model directory.
            raise SttError("Whisper local model files are missing or empty: " + ", ".join(missing)
                           + ". Run scripts/setup.py before recognition.")
        offline_environment()
        if device == "cuda":
            _prepare_windows_cuda()
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
        except (ImportError, OSError) as error:
            raise SttError("Whisper candidate dependencies are unavailable; use its isolated environment.") from error
        compute_type = "float16" if device == "cuda" else "int8"
        try:
            if device == "cuda" and ctranslate2.get_cuda_device_count() < 1:
                raise SttError("CTranslate2 cannot see a CUDA GPU; CPU fallback refused.")
            if compute_type not in ctranslate2.get_supported_compute_types(device, 0):
                raise SttError(f"CTranslate2 cannot execute {compute_type} on {device}; fallback refused.")
            model = WhisperModel(str(directory), device=device, device_index=0,
                                 compute_type=compute_type, cpu_threads=4, num_workers=1,
                                 local_files_only=True)
        except SttError:
            raise
        except (RuntimeError, ValueError, OSError) as error:
            raise SttError(f"Whisper {device} initialization failed: {error}") from error
        runtime = model.model
        actual_type = runtime.compute_type
        accepted_types = {"float16"} if device == "cuda" else {"int8", "int8_float32"}
        if runtime.device != device or actual_type not in accepted_types or not runtime.model_is_loaded:
            runtime.unload_model()
            raise SttError(f"Whisper requested {device}/{compute_type}, but initialized "
                           f"{runtime.device}/{actual_type}; fallback refused.")
        self.name = name
        self.device = device
        self.model = model
        self.sessions = {}
        # The library keeps only the first 223 hotword tokens. Prioritize the
        # user's configured aliases and sites, never a benchmark's expected text.
        terms = list(dict.fromkeys((*catalog.aliases, *catalog.aliases.values(),
                                   *catalog.sites, *keyterms(catalog))))
        self.hotwords = ", ".join(value.strip() for value in terms if value.strip())
        self.runtime_details = {
            "requested_device": device, "actual_device": runtime.device,
            "requested_compute_type": compute_type, "actual_compute_type": actual_type,
            "device_index": list(runtime.device_index), "model_is_loaded": runtime.model_is_loaded,
            "language": "en", "task": "transcribe", "temperature": 0.0,
            "condition_on_previous_text": False, "beam_size": 5,
            "hotwords_requested": len(terms), "hotwords_token_limit": model.max_length // 2 - 1,
            "hotwords_order": "aliases, alias targets, sites, remaining catalog terms",
            "segmentation": "existing Silero thresholds and endpoint; internal Whisper VAD disabled",
        }
        try:
            self.vad = Silero(vad_directory)
        except BaseException:
            runtime.unload_model()
            self.model = None
            raise
        self.emit = None

    def _decode(self, end, final):
        if self.active_start is None:
            return
        began = time.perf_counter()
        try:
            segments, _ = self.model.transcribe(
                self.audio[self.active_start:end], language="en", task="transcribe",
                temperature=0.0, condition_on_previous_text=False,
                beam_size=5, vad_filter=False, hotwords=self.hotwords or None,
            )
            # Transcription is lazy. Consume every segment before timestamping
            # final text, preserving internal capitals, punctuation and spacing.
            text = "".join(segment.text for segment in segments).strip()
        except (RuntimeError, ValueError, OSError) as error:
            raise SttError(f"Whisper {self.device} transcription failed: {error}") from error
        cost = time.perf_counter() - began
        if text != self.last_text or final:
            self.emit(TextEvent(str(self.line), text, final, self.active_start, end,
                                time.perf_counter(), self.name, self.device))
            self.last_text = text
        self.next_partial = self.processed + int(max(0.8, cost) * SAMPLE_RATE)

    def close(self):
        try:
            model = getattr(self, "model", None)
            if model is not None:
                model.model.unload_model()
        finally:
            super().close()
