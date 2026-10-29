"""Medium uses the native streaming path, local hashes and explicit architecture."""

import hashlib
import importlib.util
import json
import sys
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar import cli
from voicebar.catalog import Catalog
from voicebar.stt import BACKEND_NAMES, load_backend, model_assets
from voicebar.stt.base import SttError, keyterms
from voicebar.stt.moonshine import MoonshineBackend


class FakeArch(IntEnum):
    SMALL_STREAMING = 4
    MEDIUM_STREAMING = 5


@pytest.fixture
def native(monkeypatch):
    calls = []

    class FakeStream:
        def add_listener(self, listener):
            self.listener = listener

        def start(self):
            calls.append("start")

        def add_audio(self, audio, *, sample_rate):
            calls.append(("audio", audio, sample_rate))
            self.listener.on_line_text_changed(SimpleNamespace(line=SimpleNamespace(
                line_id=7, start_time=0.25, duration=0.5, text="Open Chrome")))

        def stop(self):
            self.listener.on_line_completed(SimpleNamespace(line=SimpleNamespace(
                line_id=7, start_time=0.25, duration=0.5, text="Open Chrome.")))

        def close(self):
            calls.append("stream closed")

    class FakeTranscriber:
        def __init__(self, **options):
            calls.append(("construct", options))

        def set_keyterms(self, terms):
            calls.append(("keyterms", terms))

        def create_stream(self, **options):
            calls.append(("stream", options))
            return FakeStream()

        def close(self):
            calls.append("transcriber closed")

    monkeypatch.setitem(sys.modules, "moonshine_voice", SimpleNamespace(
        ModelArch=FakeArch, Transcriber=FakeTranscriber))
    monkeypatch.setitem(sys.modules, "moonshine_voice.transcriber", SimpleNamespace(TranscriptEventListener=object))
    return calls


@pytest.mark.parametrize("name,architecture", [("moonshine", FakeArch.SMALL_STREAMING),
                                               ("moonshine-medium", FakeArch.MEDIUM_STREAMING)])
def test_architecture_is_explicit_with_unchanged_native_options(native, tmp_path, name, architecture):
    catalog = Catalog(apps=["Chrome", "Claude"], aliases={"cloud": "Claude"}, sites={"youtube": "https://youtube.com"})
    backend = MoonshineBackend(tmp_path, catalog, name=name)
    assert native == [("construct", {"model_path": tmp_path, "model_arch": architecture, "update_interval": 0.1,
                                     "options": {"keyterm_boost": 2.0, "return_audio_data": "false"}}),
                      ("keyterms", keyterms(catalog))]
    assert backend.name == name
    assert backend.device == "cpu"
    assert backend.partials_kind == "native"
    assert backend.runtime_details["architecture"] == architecture.name
    assert backend.runtime_details["thread_configuration"] == "native default; no override"
    backend.close()


def test_unknown_architecture_refused_before_library_import(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "moonshine_voice", None)
    with pytest.raises(SttError, match="Unknown Moonshine backend"):
        MoonshineBackend(tmp_path, Catalog(), name="moonshine-unknown")


def test_medium_stream_events_preserve_text_timing_and_name(native, tmp_path):
    backend = MoonshineBackend(tmp_path, Catalog(), name="moonshine-medium")
    events = []
    backend.start(events.append)
    backend.feed(np.zeros(512, dtype=np.float32))
    backend.finish()
    assert [(event.text, event.final) for event in events] == [("Open Chrome", False), ("Open Chrome.", True)]
    assert all((event.line_id, event.start_sample, event.end_sample, event.backend, event.device) ==
               ("7", 4000, 12000, "moonshine-medium", "cpu") for event in events)
    assert ("stream", {"update_interval": 0.1}) in native
    assert ("audio", [0.0] * 512, 16000) in native
    assert backend.stream is None
    backend.close()
    assert native[-2:] == ["stream closed", "transcriber closed"]


def test_medium_assets_only_need_the_native_directory():
    assert "moonshine-medium" in BACKEND_NAMES
    assert model_assets("moonshine-medium", "cpu") == ("moonshine-medium",)


@pytest.mark.parametrize("device", ["cuda", "auto", "mps"])
def test_medium_refuses_unsupported_devices_before_asset_io(tmp_path, monkeypatch, device):
    from voicebar.stt import models

    monkeypatch.setattr(models, "require_model", lambda *_a, **_k: pytest.fail("Unexpected model IO"))
    with pytest.raises(SttError, match="CPU-only"):
        load_backend("moonshine-medium", tmp_path, Catalog(), device=device)


def test_medium_missing_model_refused_before_native_import(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "moonshine_voice", None)
    with pytest.raises(SttError, match="moonshine-medium model is unavailable"):
        load_backend("moonshine-medium", tmp_path, Catalog())


def test_medium_factory_hashes_its_asset_without_silero(tmp_path, monkeypatch):
    from voicebar.stt import models

    calls = []
    catalog = Catalog()
    monkeypatch.setattr(models, "require_model", lambda root, name, **options:
                        calls.append(("asset", root, name, options)) or tmp_path / name)
    marker = object()
    monkeypatch.setitem(sys.modules, "voicebar.stt.moonshine", SimpleNamespace(
        MoonshineBackend=lambda *args, **kwargs: calls.append(("backend", args, kwargs)) or marker))
    assert load_backend("moonshine-medium", tmp_path, catalog) is marker
    assert calls == [("asset", tmp_path, "moonshine-medium", {"verify_hashes": True}),
                     ("backend", (tmp_path / "moonshine-medium", catalog), {"name": "moonshine-medium"})]


@pytest.fixture
def command_root(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("stt:\n  backend: null\n", encoding="utf-8")
    monkeypatch.setattr(cli, "load_catalog", lambda *_a, **_k: SimpleNamespace(catalog=Catalog(apps=["Notepad"])))
    return tmp_path


def test_medium_wav_choice_is_dry_run_and_preserves_pending_selection(command_root, monkeypatch, capsys):
    from voicebar import stt
    from voicebar.stt import base

    class FakeBackend:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    calls = []
    monkeypatch.setattr(stt, "load_backend", lambda name, *_a, **kwargs:
                        calls.append((name, kwargs)) or FakeBackend())
    monkeypatch.setattr(base, "transcribe_wav", lambda *_a, **_k: {"text": "open Notepad"})
    monkeypatch.setitem(sys.modules, "voicebar.executor", None)
    assert cli.main(["wav", "saved.wav", "--backend", "moonshine-medium", "--dry-run"],
                    project_root=command_root) == 0
    assert json.loads(capsys.readouterr().out) == {"text": "open Notepad", "executed": False,
                                                 "plan": [{"action": "open_app", "app": "Notepad"}]}
    assert calls == [("moonshine-medium", {"device": "cpu"})]
    assert cli.speech_settings(command_root)[0]["backend"] is None


def test_medium_cannot_enable_unselected_live_execution(command_root, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "voicebar.ptt", None)
    assert cli.main(["listen", "--ptt", "--backend", "moonshine-medium"], project_root=command_root) == 2
    assert "accuracy gate is pending" in capsys.readouterr().err


def test_medium_explicit_dry_run_and_settings_are_supported(command_root, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "voicebar.ptt", SimpleNamespace(listen_ptt=lambda *_a, **kw: calls.append(kw)))
    assert cli.main(["listen", "--ptt", "--dry-run", "--backend", "moonshine-medium"],
                    project_root=command_root) == 0
    assert calls[0]["backend_name"] == "moonshine-medium"
    assert calls[0]["device"] == "cpu"
    assert calls[0]["dry_run"] is True
    (command_root / "config/settings.yaml").write_text("stt:\n  backend: moonshine-medium\n  device: cpu\n", encoding="utf-8")
    assert cli.speech_settings(command_root) == ({"backend": "moonshine-medium", "device": "cpu"}, {})


@pytest.fixture
def setup_module():
    source = Path(__file__).resolve().parents[1] / "scripts/setup.py"
    spec = importlib.util.spec_from_file_location("voicebar_setup_medium_tests", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_root(tmp_path, monkeypatch, setup_module):
    entries = {}
    for name in setup_module.CPU_MODELS:
        directory = tmp_path / "models" / name
        directory.mkdir(parents=True)
        (directory / "weights").write_bytes(name.encode())
        entries[name] = {"path": name, "files": setup_module.file_manifest(directory)}
    (tmp_path / "models/manifest.json").write_text(json.dumps({"models": entries}), encoding="utf-8")
    # Every network-facing path is fake; setup may instantiate this inert API.
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda **_k: SimpleNamespace(model_info=lambda *_a, **_k: pytest.fail("Unexpected HF lookup")),
        hf_hub_download=lambda *_a, **_k: pytest.fail("Unexpected HF download")))
    monkeypatch.setattr(setup_module, "version", lambda _name: "0.1.5")
    calls = []

    def model_info(language, architecture):
        calls.append(("info", language, architecture))
        return {"download_url": setup_module.MOONSHINE_MEDIUM_URL, "model_arch": architecture, "language": language}

    def download(info, *, cache_root):
        calls.append(("download", info["language"], info["model_arch"], cache_root))
        directory = cache_root / "medium-streaming-en" / "quantized_26_08_21"
        directory.mkdir(parents=True)
        (directory / "encoder.ort").write_bytes(b"fake medium weights")
        (directory / "tokenizer.bin").write_bytes(b"fake tokenizer")
        return str(directory), FakeArch.MEDIUM_STREAMING

    monkeypatch.setitem(sys.modules, "moonshine_voice", SimpleNamespace(ModelArch=FakeArch))
    monkeypatch.setitem(sys.modules, "moonshine_voice.download", SimpleNamespace(
        find_model_info=model_info, download_model_from_info=download))
    return tmp_path, calls, entries


def test_setup_medium_adds_separate_pinned_native_assets_and_is_repeatable(setup_root, setup_module):
    root, calls, before = setup_root
    assert "moonshine-medium" in setup_module.CANDIDATE_NAMES
    setup_module.setup(root, candidates=("moonshine-medium",))
    manifest = json.loads((root / "models/manifest.json").read_text(encoding="utf-8"))
    assert all(manifest["models"][name] == entry for name, entry in before.items())
    entry = manifest["models"]["moonshine-medium"]
    assert entry["architecture"] == "MEDIUM_STREAMING"
    assert entry["source"] == setup_module.MOONSHINE_MEDIUM_URL
    assert entry["revision"] == "quantized_26_08_21"
    assert entry["catalog_package"] == "moonshine-voice 0.1.5"
    assert entry["license"] == "MIT"
    assert entry["files"] == setup_module.file_manifest(root / "models" / entry["path"])
    assert calls == [("info", "en", FakeArch.MEDIUM_STREAMING),
                     ("download", "en", FakeArch.MEDIUM_STREAMING, root / "models/moonshine-medium")]
    setup_module.setup(root, candidates=("moonshine-medium",))
    assert len(calls) == 2


def test_setup_refuses_changed_baseline_before_download(setup_root, setup_module):
    root, calls, _ = setup_root
    (root / "models/moonshine/weights").write_bytes(b"x" * len(b"moonshine"))
    with pytest.raises(SttError, match="checksum failed"):
        setup_module.setup(root, candidates=("moonshine-medium",))
    assert calls == []


@pytest.mark.parametrize("change", ["version", "url", "catalog architecture", "architecture"])
def test_setup_drift_cannot_publish_a_medium_manifest(setup_root, setup_module, monkeypatch, change):
    root, calls, _ = setup_root
    original = (root / "models/manifest.json").read_bytes()
    fake_download = sys.modules["moonshine_voice.download"]
    if change == "version":
        monkeypatch.setattr(setup_module, "version", lambda _name: "future")
    elif change == "url":
        monkeypatch.setattr(fake_download, "find_model_info", lambda *_a: {"download_url": "https://changed/model"})
    elif change == "catalog architecture":
        monkeypatch.setattr(fake_download, "find_model_info", lambda *_a: {
            "download_url": setup_module.MOONSHINE_MEDIUM_URL, "model_arch": FakeArch.SMALL_STREAMING, "language": "en"})
    else:
        monkeypatch.setattr(fake_download, "download_model_from_info", lambda *_a, **_k: (str(root), FakeArch.SMALL_STREAMING))
    with pytest.raises(ValueError, match="verified|URL changed|different architecture"):
        setup_module.setup(root, candidates=("moonshine-medium",))
    assert not any(call[0] == "download" for call in calls)
    assert (root / "models/manifest.json").read_bytes() == original


def test_medium_modified_weight_is_rejected_before_native_load(tmp_path, monkeypatch):
    directory = tmp_path / "models/moonshine-medium"
    directory.mkdir(parents=True)
    original = b"original"
    (directory / "weights").write_bytes(b"modified")
    document = {"models": {"moonshine-medium": {"path": "moonshine-medium", "files": [
        {"path": "weights", "size": len(original), "sha256": hashlib.sha256(original).hexdigest()},
    ]}}}
    (tmp_path / "models/manifest.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "moonshine_voice", None)
    with pytest.raises(SttError, match="checksum failed"):
        load_backend("moonshine-medium", tmp_path, Catalog())
