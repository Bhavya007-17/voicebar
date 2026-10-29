"""Corpus provenance and WAV format tests; no speech engine or microphone calls."""

import ast
import hashlib
import importlib.util
import json
import subprocess
import wave
from collections import Counter
from pathlib import Path

import pytest

from voicebar.catalog import Catalog
from voicebar.parser import parse
from voicebar.stt.corpus import (
    CorpusError,
    build_synthetic_manifest,
    load_contract,
    save_manifest,
    validate_wav,
    write_empty_utterance,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "test_parser_contract.py"


def test_every_contract_case_has_both_rates_and_original_provenance():
    contract = load_contract(CONTRACT)
    module = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    lengths = {
        node.targets[0].id: len(node.value.elts)
        for node in module.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"COMMANDS", "NOT_COMMANDS"}
    }
    assert Counter(case.kind for case in contract.cases) == {
        "command": lengths["COMMANDS"], "rejected": lengths["NOT_COMMANDS"], "context": 4,
    }
    manifest = build_synthetic_manifest(ROOT, contract.catalog)
    assert len(manifest["items"]) == len(contract.cases) * 2 == 288
    assert len({item["id"] for item in manifest["items"]}) == 288
    raw_hash = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    assert contract.sha256 == raw_hash == manifest["contract_sha256"]
    assert CONTRACT.read_bytes() == (ROOT / "seed" / "tests" / "test_parser_contract.py").read_bytes()
    cases = {case.id: case for case in contract.cases}
    for item in manifest["items"]:
        case = cases[item["source"]["case_id"]]
        assert item["text"] == case.text
        assert item["reference_plan"] == case.reference_plan
        assert item["expected_plan"] == case.reference_plan
        assert item["reference_last_app"] == item["last_app"] == case.reference_last_app
        assert item["contract_sha256"] == raw_hash
        assert Path(item["wav"]).name == item["wav"] == f"{item['id']}.wav"
        assert item["source"] == {
            "file": "tests/test_parser_contract.py", "kind": case.kind,
            "index": case.index, "case_id": case.id,
        }
    assert Counter(item["source"]["case_id"] for item in manifest["items"]) == {
        case.id: 2 for case in contract.cases
    }
    assert {item["rate"] for item in manifest["items"]} == {0, 2}
    silence = [item for item in manifest["items"] if item["audio_kind"] == "silence"]
    assert len(silence) == 2
    assert all(item["text"] == "" and item["reference_plan"] is None for item in silence)


def test_installed_catalog_expectations_are_separate_from_reference():
    catalog = Catalog(apps=["Notepad"], engines={"google": "https://example.com/?q={q}"})
    manifest = build_synthetic_manifest(ROOT, catalog)
    for item in manifest["items"]:
        if item["source"]["kind"] != "context":
            assert item["expected_plan"] == parse(item["text"], catalog)
    chrome = next(item for item in manifest["items"] if item["text"] == "open chrome")
    assert chrome["expected_plan"] is None
    assert chrome["reference_plan"] == [{"action": "open_app", "app": "Google Chrome"}]
    context = next(item for item in manifest["items"] if item["source"]["kind"] == "context")
    assert not context["context_available"]
    assert context["last_app"] is None
    assert context["reference_last_app"] == "Autodesk Fusion"
    assert context["expected_plan"] is None
    assert context["reference_plan"][0]["target"] == "Autodesk Fusion"


def test_available_context_uses_installed_app_identity():
    catalog = Catalog(apps=["Autodesk Fusion 2026"], aliases={"Autodesk Fusion": "Autodesk Fusion 2026"})
    manifest = build_synthetic_manifest(ROOT, catalog)
    context = next(item for item in manifest["items"] if item["source"]["case_id"] == "context-001")
    assert context["context_available"]
    assert context["last_app"] == "Autodesk Fusion 2026"
    assert context["expected_plan"] == [{"action": "window", "op": "maximize", "target": "Autodesk Fusion 2026"}]
    assert context["reference_plan"][0]["target"] == "Autodesk Fusion"


@pytest.mark.parametrize("replacement", [
    'CATALOG = __import__("os")',
    'CATALOG = Catalog.__class__',
    'CATALOG = [item for item in []]',
])
def test_projection_refuses_executable_data(tmp_path, replacement):
    text = CONTRACT.read_text(encoding="utf-8")
    module = ast.parse(text)
    catalog = next(node for node in module.body if isinstance(node, ast.Assign)
                   and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "CATALOG")
    lines = text.splitlines(keepends=True)
    altered = "".join(lines[:catalog.lineno - 1]) + replacement + "\n" + "".join(lines[catalog.end_lineno:])
    path = tmp_path / "contract.py"
    path.write_text(altered, encoding="utf-8")
    with pytest.raises(CorpusError, match="Unsupported|only Catalog"):
        load_contract(path)


def test_projection_does_not_import_or_run_test_code(tmp_path):
    # Deliberately hostile top-level code is outside the data projection. This
    # proves corpus loading does not side-load pytest or invoke test functions.
    text = CONTRACT.read_text(encoding="utf-8")
    text = 'raise RuntimeError("top-level execution")\n' + text
    text += '\ndef test_must_not_run():\n    raise RuntimeError("test execution")\n'
    path = tmp_path / "contract.py"
    path.write_text(text, encoding="utf-8")
    assert len(load_contract(path).cases) == 144


def _wav(path, *, channels=1, width=2, rate=16000, frames=b"\0\0" * 160):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(frames)


def test_empty_utterance_is_real_finite_pcm_silence(tmp_path):
    path = tmp_path / "silence.wav"
    write_empty_utterance(path)
    info = validate_wav(path)
    assert info["frames"] == 1600
    assert info["duration_seconds"] == 0.1
    assert info["wav_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with wave.open(str(path), "rb") as wav:
        assert wav.readframes(1600) == b"\0\0" * 1600


@pytest.mark.parametrize("options", [
    {"channels": 2}, {"width": 1}, {"rate": 8000}, {"frames": b""},
])
def test_wav_rejects_wrong_format_or_empty(tmp_path, options):
    path = tmp_path / "bad.wav"
    _wav(path, **options)
    with pytest.raises(CorpusError, match="PCM16|empty"):
        validate_wav(path)


def test_wav_rejects_truncated_data(tmp_path):
    path = tmp_path / "truncated.wav"
    _wav(path)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(CorpusError, match="truncated"):
        validate_wav(path)


def test_manifest_is_utf8_json_with_relative_wav_paths(tmp_path):
    manifest = build_synthetic_manifest(ROOT, Catalog())
    manifest["test_unicode"] = "café Ελληνικά 中文 🚀"
    path = tmp_path / "manifest.json"
    save_manifest(path, manifest)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert not path.with_suffix(".json.tmp").exists()


def _synthesis_module():
    spec = importlib.util.spec_from_file_location("stage3_synthesis", ROOT / "scripts" / "synthesize_stage3.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_synthesis_passes_literal_json_and_validates_all_output(tmp_path, monkeypatch):
    module = _synthesis_module()
    utterance = 'type café "$([secret])"; `not-a-command` 🚀'
    manifest = {
        "voice": "Microsoft David Desktop",
        "items": [
            {"id": "command-001-rate0", "text": utterance, "rate": 0, "wav": "command-001-rate0.wav"},
            {"id": "rejected-001-rate2", "text": "", "rate": 2, "wav": "rejected-001-rate2.wav"},
        ],
    }
    monkeypatch.setattr(module, "build_synthetic_manifest", lambda *_: manifest)
    called = []

    def run(argv, **kwargs):
        called.append((argv, kwargs))
        assert argv[1:5] == ["-NoProfile", "-NonInteractive", "-File", str(tmp_path / "scripts" / "synthesize.ps1")]
        assert argv[5] == "-RequestPath"
        assert utterance not in argv
        assert kwargs["shell"] is False and kwargs["check"] is True
        request = json.loads(Path(argv[6]).read_text(encoding="utf-8"))
        assert request["items"] == [{
            "id": "command-001-rate0", "text": utterance, "rate": 0,
            "wav": str(tmp_path / "fixtures" / "synthetic" / "command-001-rate0.wav"),
        }]
        for item in request["items"]:
            _wav(Path(item["wav"]))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"count": 1, "voice": manifest["voice"]}), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.synthesize(tmp_path, Catalog())
    assert len(called) == 1
    assert all(item["frames"] > 0 and len(item["wav_sha256"]) == 64 for item in result["items"])
    assert json.loads((tmp_path / "fixtures" / "synthetic" / "manifest.json").read_text(encoding="utf-8")) == result


def test_synthesis_never_publishes_manifest_on_incomplete_output(tmp_path, monkeypatch):
    module = _synthesis_module()
    monkeypatch.setattr(module, "build_synthetic_manifest", lambda *_: {
        "voice": "Microsoft David Desktop",
        "items": [{"id": "command-001-rate0", "text": "open chrome", "rate": 0, "wav": "command-001-rate0.wav"}],
    })
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(
        argv, 0, json.dumps({"count": 1, "voice": "Microsoft David Desktop"}), ""))
    with pytest.raises(CorpusError, match="Cannot read WAV"):
        module.synthesize(tmp_path, Catalog())
    assert not (tmp_path / "fixtures" / "synthetic" / "manifest.json").exists()
