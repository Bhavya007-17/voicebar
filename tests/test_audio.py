"""Segmentation safety tests use generated samples and fake VAD; no microphone."""

import wave
from pathlib import Path

import numpy as np
import pytest

from voicebar.audio import FRAME_SAMPLES, AudioError, SpeechSegmenter, wav_segments
from voicebar.stt.base import SAMPLE_RATE, SttError


class FakeVad:
    def __init__(self):
        self.resets = 0
        self.frames = []

    def reset(self):
        self.resets += 1

    def probability(self, frame):
        assert frame.dtype == np.float32
        assert len(frame) == FRAME_SAMPLES
        self.frames.append(frame.copy())
        return float(frame[0])


def pcm(*runs):
    return np.concatenate([np.full(FRAME_SAMPLES * count, probability, np.float32)
                           for count, probability in runs])


def replay(audio, chunk_size, **kwargs):
    segmenter = SpeechSegmenter(FakeVad(), **kwargs)
    segments = []
    for offset in range(0, len(audio), chunk_size):
        segments.extend(segmenter.feed(audio[offset:offset + chunk_size], 100 + offset / SAMPLE_RATE))
    segments.extend(segmenter.flush())
    return segments


def test_chunk_boundaries_preserve_audio_and_interval():
    audio = pcm((5, 0), (4, .9), (10, 0), (3, .9), (10, 0))
    expected = replay(audio, FRAME_SAMPLES)
    actual = replay(audio, 113)
    assert len(actual) == len(expected) == 2
    for observed, reference in zip(actual, expected, strict=True):
        assert (observed.start, observed.end, observed.speech_end) == pytest.approx(
            (reference.start, reference.end, reference.speech_end))
        np.testing.assert_array_equal(observed.audio, reference.audio)
    assert actual[0].start == 100
    assert actual[0].speech_end == pytest.approx(100 + 9 * FRAME_SAMPLES / SAMPLE_RATE)
    assert actual[0].end == pytest.approx(100 + 19 * FRAME_SAMPLES / SAMPLE_RATE)
    assert len(actual[0].audio) == 19 * FRAME_SAMPLES


def test_silero_onset_and_release_hysteresis():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0)
    # Gray probability does not start speech, but keeps an existing phrase open.
    assert segmenter.feed(pcm((2, .4)), 0) == []
    assert segmenter.last_speech_at is None
    assert segmenter.feed(pcm((1, .9), (2, .4), (9, 0)), .064) == []
    assert segmenter.last_speech_at == pytest.approx(.160)
    result = segmenter.feed(pcm((1, 0)), .448)
    assert len(result) == 1
    assert result[0].start == pytest.approx(.064)
    assert result[0].end == pytest.approx(.480)


def test_flush_preserves_real_eof_tail_without_padding_audio():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad, pre_roll_seconds=0)
    audio = np.full(FRAME_SAMPLES + 79, .9, np.float32)
    assert segmenter.feed(audio, 3) == []
    segments = segmenter.flush()
    assert len(segments) == 1
    np.testing.assert_array_equal(segments[0].audio, audio)
    assert segments[0].end == pytest.approx(3 + len(audio) / SAMPLE_RATE)
    assert segments[0].speech_end == pytest.approx(segments[0].end)
    assert len(vad.frames) == 2
    assert np.count_nonzero(vad.frames[-1]) == 79
    assert segmenter.flush() == []
    assert segmenter.last_speech_at is None


def test_reset_discards_incomplete_words_and_vad_state():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.feed(pcm((4, .9)), 10)
    segmenter.reset()  # off, lock, chime, or cancellation is a discard boundary
    assert segmenter.last_speech_at is None
    assert segmenter.feed(pcm((10, 0)), 30) == []
    assert segmenter.flush() == []
    assert vad.resets >= 3


def test_discontinuous_sample_clock_discards_old_phrase():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0)
    segmenter.feed(pcm((2, .9)), 10)
    assert segmenter.feed(pcm((10, 0)), 11) == []
    assert segmenter.flush() == []
    drops = segmenter.drain_drops()
    assert len(drops) == 1
    assert drops[0].reason == "audio_discontinuity"
    assert (drops[0].start, drops[0].end) == pytest.approx((10, 10.064))
    assert segmenter.drain_drops() == []


def test_rounding_within_two_samples_keeps_sample_clock():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0)
    segmenter.feed(pcm((1, .9)), 0)
    segmenter.feed(pcm((1, .9)), .032 + 1 / SAMPLE_RATE)
    result = segmenter.flush()
    assert len(result) == 1
    assert result[0].end == pytest.approx(.064)
    assert segmenter.drain_drops() == []


def test_oversize_phrase_is_dropped_whole_until_silence():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0,
                                max_seconds=.32, silence_seconds=.064)
    # An overlong phrase and its trailing words can never produce a split plan.
    assert segmenter.feed(pcm((100, .9)), 0) == []
    assert segmenter._active is None  # the discarded phrase consumes no PCM buffer
    assert segmenter.feed(pcm((2, 0)), 3.2) == []
    drops = segmenter.drain_drops()
    assert len(drops) == 1
    assert drops[0].reason == "maximum_duration"
    assert (drops[0].start, drops[0].end) == pytest.approx((0, 3.264))
    segments = segmenter.feed(pcm((2, .9), (2, 0)), 3.264)
    assert len(segments) == 1
    assert segments[0].start == pytest.approx(3.264)


def test_exact_cap_is_allowed_but_oversize_eof_is_not():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0,
                                max_seconds=.32, silence_seconds=.064)
    segmenter.feed(pcm((10, .9)), 0)
    assert len(segmenter.flush()) == 1
    segmenter.feed(pcm((11, .9)), 0)
    assert segmenter.flush() == []
    assert segmenter.drain_drops()[0].reason == "maximum_duration"


def test_seal_waits_for_whole_active_phrase_and_ordinary_silence_endpoint():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad, pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.080)
    assert segmenter.feed(pcm((3, .9), (1, 0)), 0) == []
    assert segmenter.sealed_at is None
    assert segmenter.excluded_samples == 0
    result = segmenter.feed(pcm((1, 0)), .128)
    assert len(result) == 1
    np.testing.assert_array_equal(result[0].audio, pcm((3, .9), (2, 0)))
    assert result[0].speech_end == pytest.approx(.096)
    assert result[0].end == pytest.approx(.160)
    assert segmenter.sealed_at == pytest.approx(result[0].end)
    assert segmenter.excluded_samples == 0
    assert len(vad.frames) == 5


def test_seal_excludes_next_utterance_in_same_callback_and_all_later_samples():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad, pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.080)
    tail = np.full(79, .9, np.float32)
    audio = np.concatenate((pcm((3, .9), (2, 0), (4, .9), (2, 0)), tail))
    result = segmenter.feed(audio, 0)
    assert len(result) == 1
    assert segmenter.sealed_at == pytest.approx(5 * FRAME_SAMPLES / SAMPLE_RATE)
    # The first excluded sample is at the cutoff, and every later sample has a
    # greater sample-clock timestamp. None belongs to the accepted [start, end).
    assert segmenter.excluded_samples == len(audio) - 5 * FRAME_SAMPLES
    assert len(vad.frames) == 5
    assert not len(segmenter._pending)
    cutoff = segmenter.sealed_at
    assert segmenter.feed(pcm((10, .9)), len(audio) / SAMPLE_RATE) == []
    assert segmenter.feed(pcm((1, .9)), 10) == []  # a later gap cannot unseal
    assert segmenter.excluded_samples == len(audio) + 6 * FRAME_SAMPLES
    assert segmenter.sealed_at == cutoff
    assert len(vad.frames) == 5


def test_silence_seals_at_first_real_full_frame_after_minimum():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.request_seal(.070)
    assert segmenter.feed(pcm((6, 0), (3, .9)), 0) == []
    assert segmenter.sealed_at == pytest.approx(.096)
    assert segmenter.excluded_samples == 6 * FRAME_SAMPLES
    assert len(vad.frames) == 3
    assert segmenter.last_speech_at is None


def test_invalid_post_seal_input_cannot_unseal_or_count_pre_cutoff_samples():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.request_seal(.010)
    segmenter.feed(pcm((1, 0)), 0)
    with pytest.raises(AudioError, match="timestamp"):
        segmenter.feed(pcm((1, .9)), float("nan"))
    with pytest.raises(SttError, match="Audio must"):
        segmenter.feed(np.array([np.nan]), .032)
    with pytest.raises(AudioError, match="precede the cutoff"):
        segmenter.feed(pcm((1, .9)), 0)
    assert segmenter.sealed_at == pytest.approx(.032)
    assert segmenter.excluded_samples == 0
    assert segmenter.feed(pcm((1, .9)), .032 - 1 / SAMPLE_RATE) == []
    assert segmenter.excluded_samples == FRAME_SAMPLES
    assert len(vad.frames) == 1


def test_continuous_and_overlong_speech_cannot_seal_until_real_quiet_endpoint():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad, pre_roll_seconds=0,
                                silence_seconds=.064, max_seconds=.128)
    segmenter.request_seal(.050)
    assert segmenter.feed(pcm((12, .9)), 0) == []
    assert segmenter.sealed_at is None
    assert segmenter.excluded_samples == 0
    assert segmenter._dropping
    assert segmenter.feed(pcm((1, 0)), .384) == []
    assert segmenter.sealed_at is None
    assert segmenter.feed(pcm((1, 0), (2, .9)), .416) == []
    assert segmenter.sealed_at == pytest.approx(.448)
    assert segmenter.excluded_samples == 2 * FRAME_SAMPLES
    assert len(vad.frames) == 14
    drops = segmenter.drain_drops()
    assert len(drops) == 1
    assert (drops[0].reason, drops[0].start) == ("maximum_duration", 0)
    assert drops[0].end == pytest.approx(segmenter.sealed_at)


def test_partial_quiet_frame_must_be_completed_by_real_samples_before_sealing():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.request_seal(.010)
    assert segmenter.feed(np.zeros(79, np.float32), 0) == []
    assert segmenter.sealed_at is None
    assert segmenter.excluded_samples == 0
    assert vad.frames == []
    tail = np.concatenate((np.zeros(FRAME_SAMPLES - 79, np.float32),
                           np.full(73, .9, np.float32)))
    assert segmenter.feed(tail, 79 / SAMPLE_RATE) == []
    assert segmenter.sealed_at == pytest.approx(.032)
    assert segmenter.excluded_samples == 73
    assert len(vad.frames) == 1
    assert not np.any(vad.frames[0])
    assert not len(segmenter._pending)


def test_partial_active_frame_is_preserved_until_natural_endpoint():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.020)
    first = np.full(FRAME_SAMPLES + 79, .9, np.float32)
    assert segmenter.feed(first, 0) == []
    assert segmenter.sealed_at is None
    tail = np.concatenate((np.full(FRAME_SAMPLES - 79, .9, np.float32),
                           pcm((2, 0)), np.full(73, .9, np.float32)))
    result = segmenter.feed(tail, len(first) / SAMPLE_RATE)
    assert len(result) == 1
    np.testing.assert_array_equal(result[0].audio, pcm((2, .9), (2, 0)))
    assert segmenter.sealed_at == pytest.approx(.128)
    assert segmenter.excluded_samples == 73


def test_seal_request_is_idempotent_and_reset_clears_request_cutoff_and_count():
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.010)
    segmenter.request_seal(.010)
    with pytest.raises(AudioError, match="cannot change"):
        segmenter.request_seal(.005)
    assert segmenter.feed(pcm((2, 0)), 0) == []
    assert segmenter.sealed_at == pytest.approx(.032)
    assert segmenter.excluded_samples == FRAME_SAMPLES
    segmenter.request_seal(.010)
    with pytest.raises(AudioError, match="cannot change"):
        segmenter.request_seal(.100)
    segmenter.reset()
    assert segmenter.sealed_at is None
    assert segmenter.excluded_samples == 0
    result = segmenter.feed(pcm((2, .9), (2, 0)), 1)
    assert len(result) == 1
    assert segmenter.sealed_at is None
    segmenter.request_seal(2)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "later"])
def test_invalid_seal_request_preserves_active_speech_and_existing_request(bad):
    segmenter = SpeechSegmenter(FakeVad(), pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.080)
    segmenter.feed(pcm((3, .9)), 0)
    with pytest.raises(AudioError, match="timestamp"):
        segmenter.request_seal(bad)
    result = segmenter.feed(pcm((2, 0)), .096)
    assert len(result) == 1
    assert segmenter.sealed_at == pytest.approx(.160)


def test_flush_cannot_force_sealing_of_active_or_partial_audio():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad, pre_roll_seconds=0, silence_seconds=.064)
    segmenter.request_seal(.020)
    first = np.full(FRAME_SAMPLES + 79, .9, np.float32)
    segmenter.feed(first, 0)
    with pytest.raises(AudioError, match="natural quiet boundary"):
        segmenter.flush()
    assert segmenter.sealed_at is None
    assert len(vad.frames) == 1  # flush never pads the pending frame
    tail = np.concatenate((np.full(FRAME_SAMPLES - 79, .9, np.float32), pcm((2, 0))))
    result = segmenter.feed(tail, len(first) / SAMPLE_RATE)
    assert len(result) == 1
    assert segmenter.sealed_at == pytest.approx(.128)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "now"])
def test_invalid_timestamp_discards_pending_speech(bad):
    segmenter = SpeechSegmenter(FakeVad())
    segmenter.feed(pcm((1, .9)), 0)
    with pytest.raises(AudioError, match="timestamp"):
        segmenter.feed(pcm((1, .9)), bad)
    assert segmenter.flush() == []


@pytest.mark.parametrize("bad", [np.array([np.nan]), np.array([1.1]), np.zeros((2, 2)), "bad"])
def test_invalid_audio_discards_pending_speech(bad):
    segmenter = SpeechSegmenter(FakeVad())
    segmenter.feed(pcm((1, .9)), 0)
    with pytest.raises(SttError, match="Audio must"):
        segmenter.feed(bad, .032)
    assert segmenter.flush() == []


def test_invalid_vad_probability_discards_pending_speech():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.feed(pcm((1, .9)), 0)
    vad.probability = lambda frame: float("nan")
    with pytest.raises(AudioError, match="probability"):
        segmenter.feed(pcm((1, .9)), .032)
    assert segmenter.flush() == []


def test_vad_failure_discards_pending_speech():
    vad = FakeVad()
    segmenter = SpeechSegmenter(vad)
    segmenter.feed(pcm((1, .9)), 0)

    def fail(frame):
        raise RuntimeError("inference failed")

    vad.probability = fail
    with pytest.raises(AudioError, match="VAD failed"):
        segmenter.feed(pcm((1, .9)), .032)
    assert segmenter.flush() == []


def test_wav_replay_uses_same_segmenter_and_sample_offsets(tmp_path: Path):
    path = tmp_path / "generated.wav"
    audio = pcm((5, 0), (4, .9), (10, 0))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes((audio * 32767).astype("<i2").tobytes())
    segments = list(wav_segments(path, segmenter=SpeechSegmenter(FakeVad()), started_at=12))
    assert len(segments) == 1
    assert segments[0].start == 12
    assert segments[0].speech_end == pytest.approx(12 + .288)
    assert segments[0].end == pytest.approx(12 + .608)
    assert len(segments[0].audio) == len(audio)


@pytest.mark.parametrize("kwargs", [
    {"silence_seconds": 0}, {"silence_seconds": 3}, {"pre_roll_seconds": -1},
    {"pre_roll_seconds": 2}, {"max_seconds": 21}, {"max_seconds": float("nan")},
    {"max_seconds": True}, {"max_seconds": .1, "pre_roll_seconds": .2},
    {"max_seconds": .3},
])
def test_unsafe_segment_limits_are_rejected(kwargs):
    with pytest.raises(AudioError):
        SpeechSegmenter(FakeVad(), **kwargs)


def test_default_vad_requires_explicit_local_root():
    with pytest.raises(AudioError, match="Local model root"):
        SpeechSegmenter()
