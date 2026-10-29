"""Stage 4 WAV corpus provenance and evaluation; no model, audio device or UI."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.catalog import Catalog
from voicebar.stt.corpus import CorpusError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stage4_wav_gate", ROOT / "scripts" / "gate_stage4.py")
stage4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage4)


def _manifest(items):
    return {"catalog": {"apps": ["Notepad", "Calculator"], "aliases": {}, "sites": {}, "engines": {}},
            "gate_config": {"name": "computer", "names": ["computer"], "followup_seconds": 8.0},
            "pinned": ["Notepad"], "test_pinned": ["Notepad", "Calculator"], "items": items,
            "personal_wake_inventory": {"wake_name_prompts": []}}


def _item(identifier, kind, text, expected=None):
    return {"id": identifier, "kind": kind, "text": text, "expected_plan": expected}


def _row(identifier, text, end):
    return {"source_ids": [identifier], "text": text, "start": end - .5, "end": end,
            "speech_end": end, "segment_id": identifier, "decode_ms": 1.0}


def test_conversation_is_literal_data_and_does_not_execute_contract(tmp_path):
    path = tmp_path / "contract.py"
    path.write_text('raise RuntimeError("must not run")\nCONVERSATION = ["hello there"]\n', encoding="utf-8")
    assert stage4.conversation_from_contract(path) == ["hello there"]
    path.write_text('CONVERSATION = __import__("os").environ\n', encoding="utf-8")
    with pytest.raises(CorpusError, match="literal"):
        stage4.conversation_from_contract(path)


def test_identity_checks_gate_and_both_contracts_without_modification(tmp_path):
    for actual, seed in stage4.IDENTICAL_FILES:
        for relative in (actual, seed):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"identical\r\n")
    assert len(stage4.check_contract_identity(tmp_path)) == 3
    (tmp_path / "voicebar/gate.py").write_bytes(b"identical\n")
    with pytest.raises(CorpusError, match="byte-identical"):
        stage4.check_contract_identity(tmp_path)


def test_manifest_contains_every_conversation_ten_long_sentences_and_both_command_forms():
    catalog = Catalog(apps=["Notepad", "Calculator"])
    config = {"name": "computer", "names": ["computer"], "followup_seconds": 8.0}
    manifest = stage4.build_manifest(ROOT, catalog, ["Notepad"], config)
    conversations = stage4.conversation_from_contract(ROOT / "tests/test_gate_contract.py")
    assert len(stage4.LONG_CONVERSATION) == 10
    assert len(manifest["items"]) == (len(conversations) + 10 + 2 * len(stage4.COMMANDS)) * 2
    assert len({item["id"] for item in manifest["items"]}) == len(manifest["items"])
    assert manifest["pinned"] == ["Notepad"]
    assert manifest["test_pinned"] == ["Notepad", "Calculator"]
    assert all(item["wav"] == item["id"] + ".wav" for item in manifest["items"])
    for text in conversations:
        assert {item["rate"] for item in manifest["items"] if item["text"] == text} == {0, 2}
    assert all(item["expected_plan"] is not None for item in manifest["items"] if item["kind"].endswith("command"))


def test_inventory_uses_recording_expected_text_and_reports_no_wake_audio(tmp_path):
    path = tmp_path / "fixtures/mine/session/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"completed": True, "prompts": [{"expected_text": "open Notepad"}]}), encoding="utf-8")
    result = stage4.personal_wake_inventory(tmp_path, "computer")
    assert result["completed_recording_prompts"] == 1
    assert result["wake_name_prompts"] == []
    assert "unverified" in result["limitation"]
    assert "not transcribed" in result["verified"]


def test_config_uses_exact_single_token_names_and_followup(tmp_path):
    config = tmp_path / "config/settings.yaml"
    config.parent.mkdir()
    config.write_text("name: computer\nnames: [computer, computa]\nfollowup_seconds: 3.5\n", encoding="utf-8")
    loaded = SimpleNamespace(catalog=Catalog(), pinned=[])
    assert stage4.gate_configuration(tmp_path, loaded) == {"name": "computer", "names": ["computer", "computa"], "followup_seconds": 3.5}
    config.write_text("name: computer\nnames: [computer, a computer]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="single words"):
        stage4.gate_configuration(tmp_path, loaded)
    config.write_text("name: computer\nnames: [helper]\n", encoding="utf-8")
    assert stage4.gate_configuration(tmp_path, loaded)["name"] == "helper"


def test_evaluation_preserves_gate_state_across_conversation_utterances():
    # A hallucinated wake-only output arms the next utterance. Resetting a gate
    # for each source would conceal this possible conversation false trigger.
    items = [_item("a", "contract_conversation", "ordinary talk"),
             _item("b", "long_conversation", "another ordinary sentence")]
    report = stage4.evaluate_transcripts(_manifest(items), [_row("a", "computer", 1), _row("b", "open Notepad", 2)])
    assert not report["passed"]
    for mode in ("name", "open"):
        assert report["modes"][mode]["conversation_plans"][0]["source_id"] == "b"


def test_named_commands_run_both_modes_unnamed_name_mode_is_expired():
    expected = [{"action": "open_app", "app": "Notepad"}]
    items = [_item("a", "named_command", "computer open Notepad", expected),
             _item("b", "unnamed_command", "open Notepad", expected)]
    report = stage4.evaluate_transcripts(_manifest(items), [_row("a", "Computer, open Notepad.", 1),
                                                          _row("b", "Open Notepad.", 12)])
    assert report["passed"]
    assert report["modes"]["name"]["decisions"][1]["plan"] is None
    assert report["modes"]["open"]["decisions"][1]["plan"] == expected


def test_missing_and_empty_transcripts_cannot_pass_safety_by_silence():
    items = [_item("a", "contract_conversation", "ordinary talk")]
    for transcripts in ([], [_row("a", "", 1)]):
        report = stage4.evaluate_transcripts(_manifest(items), transcripts)
        assert not report["passed"]
        assert report["modes"]["name"]["failures"][0]["check"] == "source_transcription_missing"


def test_wake_variants_are_only_observed_exact_prefixes_with_matching_command():
    item = _item("a", "named_command", "computer open Notepad", [{"action": "open_app", "app": "Notepad"}])
    config = _manifest([])["gate_config"]
    catalog = Catalog(apps=["Notepad"])
    candidate = stage4.wake_observation("Okay computa, open Notepad.", item, config, catalog)
    assert candidate["candidate_exact_variant"] == "computa"
    assert stage4.wake_observation("computa close Notepad", item, config, catalog)["candidate_exact_variant"] is None
    assert stage4.wake_observation("Computer open Notepad", item, config, catalog)["candidate_exact_variant"] is None
    assert config["names"] == ["computer"]


def test_source_mapping_uses_overlap_and_never_includes_adjacent_silence():
    segment = SimpleNamespace(start=1.5, end=2.5)
    assert stage4.source_ids(segment, [{"id": "before", "start": 0, "end": 1},
                                      {"id": "a", "start": 1.7, "end": 2.2},
                                      {"id": "after", "start": 3, "end": 4}]) == ["a"]


def test_wav_gate_uses_completed_segment_api_without_prefix_work():
    from voicebar.stt.base import TextEvent

    audio = np.full(512, 0.2, dtype=np.float32)
    calls = []

    class Backend:
        def transcribe_segment(self, received, emit):
            np.testing.assert_array_equal(received, audio)
            calls.append("complete")
            emit(TextEvent("one", "computer open Notepad", True, 0, len(audio), 100, "fake"))

        def start(self, emit):
            pytest.fail("WAV gate invoked streaming prefix decoding")

    text, _ = stage4.transcribe_segment(Backend(), SimpleNamespace(audio=audio))
    assert text == "computer open Notepad" and calls == ["complete"]


def test_continuous_audio_uses_one_segmenter_and_explicit_followup_expiry(monkeypatch):
    calls = []

    class Segmenter:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def feed(self, audio, started_at):
            calls.append(("feed", len(audio), started_at))
            return []

        def flush(self):
            calls.append(("flush",))
            return []

    monkeypatch.setitem(sys.modules, "voicebar.audio", SimpleNamespace(SpeechSegmenter=Segmenter))
    import voicebar.stt.base
    monkeypatch.setattr(voicebar.stt.base, "read_wav", lambda _: np.ones(16000, dtype=np.float32))
    manifest = _manifest([{"id": "a", "kind": "contract_conversation", "wav": "a.wav"},
                          {"id": "b", "kind": "named_command", "wav": "b.wav"}])
    assert list(stage4.corpus_segments(ROOT, manifest)) == []
    assert sum(call[0] == "init" for call in calls) == 1
    assert calls[1] == ("feed", 16000, 0.0)
    assert calls[2] == ("feed", 10400, 1.0)
    assert calls[3] == ("feed", 144000, 1.65)
    assert calls[4] == ("feed", 16000, 10.65)
    assert calls[-1] == ("flush",)
