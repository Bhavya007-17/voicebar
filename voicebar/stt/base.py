"""Streaming backend contract: normalized 16 kHz mono audio in, text events out."""

import time
import wave
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


class SttError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextEvent:
    line_id: str
    text: str
    final: bool
    start_sample: int
    end_sample: int
    emitted_at: float
    backend: str
    device: str = "cpu"


def samples(audio) -> np.ndarray:
    value = np.asarray(audio, dtype=np.float32)
    if value.ndim != 1 or not np.isfinite(value).all() or np.any(np.abs(value) > 1.001):
        raise SttError("Audio must be finite, normalized mono samples in [-1, 1].")
    return np.ascontiguousarray(value)


def read_wav(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, SAMPLE_RATE, 2, "NONE"):
                raise SttError("WAV must be 16 kHz mono PCM with 16-bit samples.")
            if wav.getnframes() > SAMPLE_RATE * 120:
                raise SttError("WAV exceeds the 120-second utterance limit; split it into speech segments.")
            data = wav.readframes(wav.getnframes())
            if len(data) != wav.getnframes() * 2 or not data:
                raise SttError("WAV is empty or truncated.")
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    except (wave.Error, EOFError) as error:
        raise SttError(f"Invalid WAV: {error}") from error


def keyterms(catalog) -> list[str]:
    return list(dict.fromkeys(
        value.strip() for value in (*catalog.apps, *catalog.aliases, *catalog.sites)
        if value.strip() and "," not in value
    ))


class Backend(ABC):
    name: str
    device = "cpu"
    hotwords_supported = False
    partials_kind = "native"

    @abstractmethod
    def start(self, emit: Callable[[TextEvent], None]) -> None: ...

    @abstractmethod
    def feed(self, audio: np.ndarray) -> None: ...

    @abstractmethod
    def finish(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def transcribe_segment(backend: Backend, audio, emit: Callable[[TextEvent], None]) -> None:
    """Transcribe already-complete audio, using final-only decoding if supported.

    Streaming backends may expose ``transcribe_segment(audio, emit)`` to omit
    provisional work. Simple backends retain their ordinary start/feed/finish
    lifecycle. Validation happens before either path starts recognition.
    """
    audio = samples(audio)
    complete = getattr(backend, "transcribe_segment", None)
    if complete is not None:
        complete(audio, emit)
        return
    backend.start(emit)
    try:
        backend.feed(audio)
        backend.finish()
    except BaseException:
        backend.close()
        raise


def transcribe_wav(backend: Backend, path: Path, *, realtime: bool = False, on_event=None,
                   chunk_samples: int | None = None) -> dict:
    """Batch replay by default; only paced replay has a release-to-final clock.

    release_to_final_ms is measured after all recorded audio has arrived. It is
    deliberately distinct from last-speech-to-final, which needs a VAD reference
    in the later benchmark. Batch decode_ms is never mislabeled EOS latency.
    """
    audio = read_wav(path)
    # Batch files need no provisional prefix work at every 32 ms. Native live
    # replay keeps exact 512-sample frames; both use the same feed/final API.
    chunk_size = chunk_samples if chunk_samples is not None else (512 if realtime else len(audio))
    if chunk_size <= 0:
        raise SttError("Replay chunk size must be positive.")
    events = []

    def emit(event):
        events.append(event)
        if on_event:
            on_event(event)

    began = time.perf_counter()
    backend.start(emit)
    try:
        for offset in range(0, len(audio), chunk_size):
            chunk = audio[offset:offset + chunk_size]
            if realtime:
                deadline = began + (offset + len(chunk)) / SAMPLE_RATE
                delay = deadline - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)  # pace recorded audio, never a readiness workaround
            backend.feed(chunk)
        # Paced samples arrive on the file's audio clock. If inference falls
        # behind that clock, its backlog must count toward release latency.
        released = began + len(audio) / SAMPLE_RATE if realtime else time.perf_counter()
        backend.finish()
    except BaseException:
        backend.close()
        raise
    ended = time.perf_counter()
    finals = {}
    for event in events:
        if event.final:
            finals[event.line_id] = event
    ordered = sorted(finals.values(), key=lambda event: (event.start_sample, event.line_id))
    return {
        "backend": backend.name, "device": backend.device,
        "text": " ".join(event.text.strip() for event in ordered if event.text.strip()),
        "audio_seconds": len(audio) / SAMPLE_RATE,
        "decode_ms": (ended - began) * 1000,
        "release_to_final_ms": max(0.0, (max((event.emitted_at for event in ordered), default=ended) - released) * 1000) if realtime else None,
        "timing_mode": "realtime-replay" if realtime else "unpaced-batch",
        "chunk_samples": chunk_size,
        "last_speech_to_final_ms": None,
        "hotwords_supported": backend.hotwords_supported,
        "partials_kind": backend.partials_kind,
        "runtime_details": dict(getattr(backend, "runtime_details", getattr(backend, "placement", {}))),
        "events": [asdict(event) for event in events],
    }
