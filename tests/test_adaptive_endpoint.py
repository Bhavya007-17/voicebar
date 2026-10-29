"""Adaptive boundaries require a complete current prefix; no devices or apps."""

import numpy as np
import pytest

from voicebar.audio import FRAME_SAMPLES, AudioError, SpeechSegmenter
from voicebar.catalog import Catalog, CatalogError
from voicebar.endpoint import EndpointSettings, PartialPolicy


class Vad:
    def reset(self):
        pass

    def probability(self, audio):
        return float(audio[0])


def audio(frames, value):
    return np.full(frames * FRAME_SAMPLES, value, dtype=np.float32)


def make():
    return SpeechSegmenter(Vad(), pre_roll_seconds=0, silence_seconds=.8,
                           short_silence_seconds=.25,
                           partial_policy=PartialPolicy(Catalog(apps=["Notepad"])))


def test_current_complete_prefix_uses_short_wait():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert segmenter.update_partial("computer open Notepad", start=0, speech_end=.128)
    assert segmenter.feed(audio(7, 0), .128) == []
    result = segmenter.feed(audio(1, 0), .352)
    assert len(result) == 1
    assert result[0].end - result[0].speech_end == pytest.approx(.256)
    assert segmenter.drain_previews() == []


@pytest.mark.parametrize("text", ["type Hello", "search google for Blue Sky",
                                 "open Notepad and type this", "start dictation"])
def test_payload_commands_keep_long_wait(text):
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert not segmenter.update_partial(text, start=0, speech_end=.128)
    assert segmenter.feed(audio(24, 0), .128) == []
    result = segmenter.feed(audio(1, 0), .896)
    assert len(result) == 1
    assert result[0].end - result[0].speech_end == pytest.approx(.8)


def test_resumed_speech_invalidates_complete_partial_until_new_text_covers_it():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert segmenter.update_partial("open Notepad", start=0, speech_end=.128)
    segmenter.feed(audio(2, 0), .128)
    segmenter.feed(audio(2, .9), .192)
    assert not segmenter.update_partial("open Notepad", start=0, speech_end=.128)
    assert segmenter.feed(audio(8, 0), .256) == []
    assert segmenter.update_partial("maximize Notepad", start=0, speech_end=.256)
    assert len(segmenter.feed(audio(1, 0), .512)) == 1


def test_free_text_protection_latches_through_a_later_bad_prefix():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert not segmenter.update_partial("type Hello", start=0, speech_end=.128)
    assert not segmenter.update_partial("open Notepad", start=0, speech_end=.128)
    assert segmenter.feed(audio(8, 0), .128) == []


def test_unknown_and_missing_partials_keep_long_wait():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert not segmenter.update_partial("open", start=0, speech_end=.128)
    assert segmenter.feed(audio(8, 0), .128) == []
    preview, = segmenter.drain_previews()
    assert (preview.start, preview.speech_end, preview.end) == pytest.approx((0, .128, .384))
    assert len(preview.audio) == 12 * FRAME_SAMPLES
    assert segmenter.preview_is_current(preview)
    segmenter.feed(audio(2, 0), .384)
    assert segmenter.drain_previews() == []


def test_completed_utterance_discards_uncollected_provisional_work():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    assert segmenter.feed(audio(25, 0), .128)
    assert segmenter.drain_previews() == []


def test_previous_utterance_partial_cannot_shorten_new_utterance():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    segmenter.reset()
    segmenter.feed(audio(4, .9), 1)
    assert not segmenter.update_partial("open Notepad", start=0, speech_end=.128)
    assert segmenter.feed(audio(8, 0), 1.128) == []


def test_dictation_always_keeps_long_wait():
    policy = PartialPolicy(Catalog(apps=["Notepad"]), dictating=True)
    assert policy("open Notepad") == "protected"


def test_whole_utterance_ambiguity_and_chains():
    policy = PartialPolicy(Catalog(apps=["Notepad", "Calculator"]))
    assert policy("hey computer open Notepad") == "complete"
    assert policy("open Notepad and maximize it") == "complete"
    assert policy("open Notepad and other conversation") == "unknown"
    assert policy("open Notepad and search google for Cats") == "protected"


def test_adaptive_reset_drops_pending_previews_and_partial_state():
    segmenter = make()
    segmenter.feed(audio(4, .9), 0)
    segmenter.feed(audio(8, 0), .128)
    segmenter.reset()
    assert segmenter.drain_previews() == []
    segmenter.feed(audio(4, .9), 2)
    assert segmenter.update_partial("open Notepad", start=2, speech_end=2.128)


@pytest.mark.parametrize("value", [0, -.1, float("nan"), float("inf"), True, ".25", .81])
def test_invalid_short_wait_is_rejected(value):
    with pytest.raises(AudioError):
        SpeechSegmenter(Vad(), silence_seconds=.8, short_silence_seconds=value)


@pytest.mark.parametrize("value", [True, float("nan"), "today"])
def test_invalid_partial_timestamp_is_rejected(value):
    with pytest.raises(AudioError):
        make().update_partial("open Notepad", start=value, speech_end=.128)


def test_endpoint_settings_are_validated_and_conservative_by_default():
    assert EndpointSettings.from_document({}) == EndpointSettings(.8, .25, False)
    assert EndpointSettings.from_document({"audio": {"adaptive_endpoint": True}}).adaptive
    for value in ({"silence_seconds": .1}, {"short_silence_seconds": True},
                  {"adaptive_endpoint": "yes"}, {"silence_seconds": float("nan")}):
        with pytest.raises(CatalogError):
            EndpointSettings.from_document({"audio": value})


def test_segmenter_never_shortens_a_sealed_or_overlong_phrase():
    segmenter = SpeechSegmenter(Vad(), pre_roll_seconds=0, silence_seconds=.8,
                               max_seconds=1, short_silence_seconds=.25,
                               partial_policy=lambda _: "complete")
    segmenter.feed(audio(4, .9), 0)
    segmenter.update_partial("open Notepad", start=0, speech_end=.128)
    segmenter.feed(audio(32, .9), .128)
    assert segmenter.feed(audio(8, 0), 1.152) == []
    assert segmenter.drain_previews() == []
    assert segmenter._dropping
