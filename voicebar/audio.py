"""Bounded speech segments shared by live capture and local WAV replay.

The controller owns the microphone stream. A reset discards every buffered word
when capture is disabled, locked, interrupted, or contaminated by a chime.
Neither inference nor this segmenter belongs in a microphone callback.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .stt.base import SAMPLE_RATE, SttError, read_wav, samples

FRAME_SAMPLES = 512


class AudioError(SttError):
    """Invalid or interrupted audio cannot produce a partial command."""


@dataclass(frozen=True)
class SpeechSegment:
    audio: np.ndarray
    start: float
    end: float
    speech_end: float


@dataclass(frozen=True)
class AudioDrop:
    reason: str
    start: float
    end: float


@dataclass(frozen=True)
class SpeechPreview:
    """Provisional PCM at a short quiet boundary; it never authorizes execution."""

    audio: np.ndarray
    start: float
    end: float
    speech_end: float


class SpeechSegmenter:
    """Silero frames with pre-roll, a silence endpoint, and a whole-utterance cap.

    ``started_at`` is the first sample's monotonic timestamp, or the WAV sample
    clock for replay. Successive input must be contiguous within two samples;
    callback arrival jitter should never be used as the sample clock.

    The injected VAD provides ``reset()`` and ``probability(frame)``. Defaults
    reuse the existing manifest-backed CPU VAD, without calling a downloader.
    """

    def __init__(
        self, vad=None, *, root: Path | None = None, silence_seconds: float = 0.30,
        pre_roll_seconds: float = 0.20, max_seconds: float = 20.0,
        short_silence_seconds: float | None = None, partial_policy=None,
    ):
        for value, label, lower, upper in (
            (silence_seconds, "silence endpoint", 0, 2),
            (pre_roll_seconds, "pre-roll", -1, 1),
            (max_seconds, "segment cap", 0, 20),
        ):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not lower < value <= upper):
                raise AudioError(f"Audio {label} is outside its supported range")
        if pre_roll_seconds < 0 or pre_roll_seconds >= max_seconds:
            raise AudioError("Audio pre-roll must be nonnegative and shorter than the segment cap")
        if silence_seconds >= max_seconds:
            raise AudioError("Audio silence endpoint must be shorter than the segment cap")
        if short_silence_seconds is not None and (
                isinstance(short_silence_seconds, bool)
                or not isinstance(short_silence_seconds, (int, float))
                or not math.isfinite(short_silence_seconds)
                or not 0 < short_silence_seconds <= silence_seconds):
            raise AudioError("Short silence must be positive and no longer than the ordinary endpoint")
        if partial_policy is not None and not callable(partial_policy):
            raise AudioError("The partial transcript policy must be callable")
        if vad is None:
            if root is None:
                raise AudioError("Local model root is required when no VAD is injected")
            from .stt.models import offline_environment, require_model
            from .stt.parakeet import Silero

            offline_environment()
            vad = Silero(require_model(Path(root), "silero", verify_hashes=True))
        self.vad = vad
        self._silence_samples = math.ceil(silence_seconds * SAMPLE_RATE)
        self._short_samples = (math.ceil(short_silence_seconds * SAMPLE_RATE)
                               if short_silence_seconds is not None else None)
        self._partial_policy = partial_policy
        self._max_samples = math.floor(max_seconds * SAMPLE_RATE)
        self._pre_roll_frames = math.ceil(pre_roll_seconds * SAMPLE_RATE / FRAME_SAMPLES)
        self._drops: list[AudioDrop] = []
        self.reset()

    @property
    def last_speech_at(self) -> float | None:
        """Last VAD-active sample timestamp; independent of STT queue progress."""
        return self._last_speech_at

    @property
    def sealed_at(self) -> float | None:
        """Sample-clock end of the first natural quiet boundary after a request."""
        return self._sealed_at

    @property
    def excluded_samples(self) -> int:
        """Samples excluded from VAD, starting at the sealed interval's end."""
        return self._excluded_samples

    def request_seal(self, after: float) -> None:
        """Stop at a real quiet full-frame boundary at or after ``after``.

        An active or overlong phrase must reach its ordinary silence endpoint.
        Samples at and after ``sealed_at`` belong to the excluded half-open
        interval, including the rest of the callback that supplied the boundary.
        The same request is idempotent; changing it requires an explicit reset.
        """
        if (isinstance(after, bool) or not isinstance(after, (int, float))
                or not math.isfinite(after)):
            raise AudioError("Audio seal timestamp must be finite")
        if self._seal_after is not None and after != self._seal_after:
            raise AudioError("Audio seal request cannot change before reset")
        self._seal_after = after

    def drain_drops(self) -> list[AudioDrop]:
        drops, self._drops = self._drops, []
        return drops

    def drain_previews(self) -> list[SpeechPreview]:
        """Transfer each short quiet boundary once to the recognition worker.

        A caller must serialize preview and final inference on its model. No
        preview is produced unless short endpoints and a policy are configured.
        Discard previews when the capture generation changes, like final audio.
        """
        previews, self._previews = self._previews, []
        return previews

    def preview_is_current(self, preview: SpeechPreview) -> bool:
        """Skip queued provisional work once its speech changed or completed."""
        return (self._active is not None and preview.start == self._active_start
                and preview.speech_end == self._last_speech_at)

    def update_partial(self, text: str, *, start: float, speech_end: float) -> bool:
        """Apply a provisional transcript only to the exact speech it covered.

        A result for an earlier utterance or a prefix before the latest speech
        can never shorten the endpoint. The classifier is pure and never calls
        the gate. Protected free text latches until the utterance is complete.
        """
        if (not isinstance(text, str) or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in (start, speech_end))):
            raise AudioError("Partial transcript requires text and finite sample-clock timestamps")
        if (self._active is None or self._active_start != start
                or self._last_speech_at != speech_end or self._partial_policy is None):
            return False
        decision = self._partial_policy(text)
        if decision == "protected":
            self._protected = True
        self._partial = (start, speech_end, decision == "complete" and not self._protected)
        return self._partial[2]

    def reset(self) -> None:
        """Forget incomplete speech and recurrent state; never emit on shutdown."""
        self.vad.reset()
        self._pending = np.empty(0, dtype=np.float32)
        self._pending_start = None
        self._next_input_at = None
        self._history = deque(maxlen=self._pre_roll_frames)
        self._active: list[np.ndarray] | None = None
        self._active_start = None
        self._active_samples = 0
        self._silent_samples = 0
        self._last_speech_at = None
        self._dropping = False
        self._seal_after = None
        self._sealed_at = None
        self._excluded_samples = 0
        self._previews: list[SpeechPreview] = []
        self._previewed_speech_end = None
        self._partial = None
        self._protected = False

    def _seal_if_quiet(self, end: float) -> bool:
        if (self._seal_after is not None and end >= self._seal_after
                and self._active is None and not self._dropping
                and not len(self._pending)):
            self._sealed_at = end
            return True
        return False

    def _clear_utterance(self) -> None:
        self._history.clear()
        self._active = None
        self._active_start = None
        self._active_samples = 0
        self._silent_samples = 0
        self._dropping = False
        self._previewed_speech_end = None
        self._partial = None
        self._protected = False
        self._previews.clear()

    def _complete(self, end: float) -> SpeechSegment | None:
        if self._dropping:
            self._drops.append(AudioDrop("maximum_duration", self._active_start, end))
            self._clear_utterance()
            return None
        if self._active is None:
            return None
        segment = SpeechSegment(np.concatenate(self._active), self._active_start,
                                end, self._last_speech_at)
        self._clear_utterance()
        return segment

    def _frame(self, audio: np.ndarray, start: float) -> SpeechSegment | None:
        end = start + len(audio) / SAMPLE_RATE
        frame = audio if len(audio) == FRAME_SAMPLES else np.pad(audio, (0, FRAME_SAMPLES - len(audio)))
        try:
            probability = float(self.vad.probability(frame))
        except Exception as error:
            self.reset()
            raise AudioError("VAD failed; incomplete audio discarded") from error
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            self.reset()
            raise AudioError("VAD returned an invalid speech probability; audio discarded")
        # Match the installed Silero implementation's 0.5 onset and 0.35 release.
        speech = probability >= (0.35 if self._active is not None or self._dropping else 0.5)
        if speech:
            self._last_speech_at = end
        if self._active is None and not self._dropping:
            if not speech:
                self._history.append((start, audio.copy()))
                return None
            self._active = [item[1] for item in self._history]
            self._active_start = self._history[0][0] if self._history else start
            self._active_samples = sum(len(item) for item in self._active)
            self._history.clear()
        if not self._dropping:
            if self._active_samples + len(audio) > self._max_samples:
                # Discard the whole phrase, then wait for its endpoint. Splitting
                # could turn trailing conversation into a standalone command.
                self._active = None
                self._active_samples = 0
                self._dropping = True
            else:
                self._active.append(audio.copy())
                self._active_samples += len(audio)
        self._silent_samples = 0 if speech else self._silent_samples + len(audio)
        short_ready = (self._short_samples is not None and self._active is not None
                       and self._silent_samples >= self._short_samples)
        short_complete = (short_ready and self._partial is not None
                          and self._partial[:2] == (self._active_start, self._last_speech_at)
                          and self._partial[2] and not self._protected)
        if (short_ready and not short_complete and not self._protected and self._partial_policy is not None
                and self._previewed_speech_end != self._last_speech_at):
            self._previewed_speech_end = self._last_speech_at
            self._previews.append(SpeechPreview(np.concatenate(self._active), self._active_start,
                                                end, self._last_speech_at))
        if short_complete or self._silent_samples >= self._silence_samples:
            return self._complete(end)
        return None

    def feed(self, audio, started_at: float) -> list[SpeechSegment]:
        """Consume normalized 16 kHz mono samples, retaining at most one phrase."""
        if (isinstance(started_at, bool) or not isinstance(started_at, (int, float))
                or not math.isfinite(started_at)):
            if self._sealed_at is None:
                self.reset()
            raise AudioError("Audio start timestamp must be finite")
        try:
            audio = samples(audio)
        except (SttError, ValueError, TypeError) as error:
            if self._sealed_at is None:
                self.reset()
            if isinstance(error, SttError):
                raise
            raise AudioError("Audio must contain normalized mono samples") from error
        if not len(audio):
            return []
        if self._sealed_at is not None:
            # Sealing stays latched even if capture later supplies a gap. No
            # post-cutoff sample may restart VAD or a new utterance.
            if abs(started_at - self._next_input_at) <= 2 / SAMPLE_RATE:
                started_at = self._next_input_at
            if started_at < self._sealed_at:
                raise AudioError("Audio after sealing cannot precede the cutoff")
            self._next_input_at = started_at + len(audio) / SAMPLE_RATE
            self._excluded_samples += len(audio)
            return []
        if self._next_input_at is not None:
            if abs(started_at - self._next_input_at) > 2 / SAMPLE_RATE:
                if self._active_start is not None:
                    self._drops.append(AudioDrop("audio_discontinuity", self._active_start,
                                                 self._next_input_at))
                self.reset()
            else:
                started_at = self._next_input_at
        self._next_input_at = started_at + len(audio) / SAMPLE_RATE
        result = []
        offset = 0
        if len(self._pending):
            take = min(FRAME_SAMPLES - len(self._pending), len(audio))
            self._pending = np.concatenate((self._pending, audio[:take]))
            offset = take
            if len(self._pending) == FRAME_SAMPLES:
                end = self._pending_start + FRAME_SAMPLES / SAMPLE_RATE
                segment = self._frame(self._pending, self._pending_start)
                if segment is not None:
                    result.append(segment)
                self._pending = np.empty(0, dtype=np.float32)
                self._pending_start = None
                if self._seal_if_quiet(end):
                    self._excluded_samples += len(audio) - offset
                    return result
        while offset + FRAME_SAMPLES <= len(audio):
            start = started_at + offset / SAMPLE_RATE
            end = start + FRAME_SAMPLES / SAMPLE_RATE
            segment = self._frame(audio[offset:offset + FRAME_SAMPLES], start)
            if segment is not None:
                result.append(segment)
            offset += FRAME_SAMPLES
            if self._seal_if_quiet(end):
                self._excluded_samples += len(audio) - offset
                return result
        if offset < len(audio):
            self._pending = audio[offset:].copy()
            self._pending_start = started_at + offset / SAMPLE_RATE
        return result

    def flush(self) -> list[SpeechSegment]:
        """Finish a known WAV boundary. Live lock/off/stop must call reset instead."""
        if self._seal_after is not None:
            raise AudioError("A seal request requires a natural quiet boundary; reset to discard")
        result = []
        if len(self._pending):
            segment = self._frame(self._pending, self._pending_start)
            if segment is not None:
                result.append(segment)
        if self._next_input_at is not None:
            segment = self._complete(self._next_input_at)
            if segment is not None:
                result.append(segment)
        self.reset()
        return result


def wav_segments(
    path: Path, *, segmenter: SpeechSegmenter | None = None,
    root: Path | None = None, started_at: float = 0.0,
) -> Iterator[SpeechSegment]:
    """Replay a validated local WAV through exactly the live segmentation path."""
    segmenter = segmenter if segmenter is not None else SpeechSegmenter(root=root)
    segmenter.reset()
    audio = read_wav(Path(path))
    try:
        for offset in range(0, len(audio), FRAME_SAMPLES):
            yield from segmenter.feed(audio[offset:offset + FRAME_SAMPLES],
                                      started_at + offset / SAMPLE_RATE)
        yield from segmenter.flush()
    finally:
        segmenter.reset()
