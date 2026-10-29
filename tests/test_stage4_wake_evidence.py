"""Personal wake evidence provenance and exact matching; no ASR or microphone."""

import importlib.util
from pathlib import Path

import pytest

from voicebar.catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stage4_personal_wake", ROOT / "scripts/transcribe_stage4_wake.py")
stage4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage4)


def test_recording_directory_accepts_only_existing_direct_mine_children(tmp_path):
    session = tmp_path / "fixtures/mine/session"
    nested = session / "nested"
    nested.mkdir(parents=True)
    assert stage4.recording_directory(tmp_path, Path("fixtures/mine/session")) == session.resolve()
    assert stage4.recording_directory(tmp_path, session) == session.resolve()
    for path in (Path("fixtures/mine"), nested, Path("fixtures/mine/missing"), Path("fixtures/mine/../../../outside")):
        with pytest.raises(ValueError, match="direct child"):
            stage4.recording_directory(tmp_path, path)


def test_personal_wake_evidence_uses_exact_recognized_prefix_and_actual_tail():
    catalog = Catalog(apps=["Notepad", "Calculator"])
    heard = stage4.wake_observation("Okay Computer. open Notepad.", catalog, {"computer"})
    assert heard == {
        "leading_token": "computer", "matched_configured_name": True,
        "exact_name": "computer", "command_tail": "open Notepad.",
        "command_tail_plan": [{"action": "open_app", "app": "Notepad"}],
    }
    # A close-sounding word is evidence of an unmatched prefix, never an alias.
    heard = stage4.wake_observation("computa open Notepad", catalog, {"computer"})
    assert heard["leading_token"] == "computa"
    assert heard["exact_name"] is None
    assert heard["command_tail_plan"] is None


def test_split_wake_and_incidental_audio_are_not_assigned_prompt_plans():
    catalog = Catalog(apps=["Notepad"])
    wake_only = stage4.wake_observation("Computer.", catalog, {"computer"})
    assert wake_only["exact_name"] == "computer"
    assert wake_only["command_tail_plan"] is None
    incidental = stage4.wake_observation("I will open Notepad after lunch", catalog, {"computer"})
    assert incidental["matched_configured_name"] is False
    assert incidental["command_tail"] is None
    assert incidental["command_tail_plan"] is None
