"""New speech candidates stay offline and cannot enable unselected execution."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from voicebar import cli
from voicebar.stt import BACKEND_NAMES, load_backend, model_assets
from voicebar.stt.base import SttError

CANDIDATES = ("qwen", "whisper-large", "whisper-turbo")


@pytest.mark.parametrize("backend,device,assets", [
    ("moonshine", "cpu", ("moonshine",)),
    ("parakeet", "cpu", ("parakeet", "silero")),
    ("parakeet", "cuda", ("parakeet-cuda", "silero")),
    *[(name, device, (name, "silero")) for name in CANDIDATES for device in ("cpu", "cuda")],
])
def test_assets_preserve_baseline_and_register_candidates(backend, device, assets):
    assert backend in BACKEND_NAMES
    assert model_assets(backend, device) == assets


def test_unknown_asset_family_refused():
    with pytest.raises(SttError, match="Unknown speech backend"):
        model_assets("remote/model", "cpu")


@pytest.mark.parametrize("name", CANDIDATES)
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_candidate_models_must_exist_before_backend_import(tmp_path, monkeypatch, name, device):
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", None)
    monkeypatch.setitem(sys.modules, "voicebar.stt.whisper", None)
    with pytest.raises(SttError, match="model is unavailable"):
        load_backend(name, tmp_path, object(), device=device)


@pytest.mark.parametrize("name", CANDIDATES)
@pytest.mark.parametrize("device", ["auto", "mps", "other"])
def test_invalid_candidate_device_refused_before_asset_io(tmp_path, monkeypatch, name, device):
    from voicebar.stt import models

    monkeypatch.setattr(models, "require_model", lambda *_a, **_k: pytest.fail("Device must fail before asset IO"))
    with pytest.raises(SttError, match="Use cpu or cuda"):
        load_backend(name, tmp_path, object(), device=device)


@pytest.mark.parametrize("name", CANDIDATES)
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_dispatch_hashes_candidate_and_vad_then_uses_local_directories(tmp_path, monkeypatch, name, device):
    from voicebar.stt import models

    catalog = object()
    calls = []
    marker = object()

    def require(root, asset, *, verify_hashes):
        calls.append(("asset", root, asset, verify_hashes))
        return tmp_path / "models" / asset

    def construct(directory, vad_directory, actual_catalog, **options):
        calls.append(("backend", directory, vad_directory, actual_catalog, options))
        return marker

    monkeypatch.setattr(models, "require_model", require)
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", SimpleNamespace(QwenBackend=construct))
    monkeypatch.setitem(sys.modules, "voicebar.stt.whisper", SimpleNamespace(WhisperBackend=construct))
    assert load_backend(name, tmp_path, catalog, device=device) is marker
    assert calls[:2] == [("asset", tmp_path, name, True), ("asset", tmp_path, "silero", True)]
    expected = {"device": device}
    if name.startswith("whisper-"):
        expected["name"] = name
    assert calls[2] == ("backend", tmp_path / "models" / name, tmp_path / "models/silero", catalog, expected)


@pytest.mark.parametrize("name", CANDIDATES)
def test_missing_silero_refuses_before_candidate_construct(tmp_path, monkeypatch, name):
    from voicebar.stt import models

    def require(root, asset, **kwargs):
        if asset == "silero":
            raise SttError("silero model is unavailable")
        return tmp_path / asset

    monkeypatch.setattr(models, "require_model", require)
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", None)
    monkeypatch.setitem(sys.modules, "voicebar.stt.whisper", None)
    with pytest.raises(SttError, match="silero model is unavailable"):
        load_backend(name, tmp_path, object())


@pytest.mark.parametrize("name", CANDIDATES)
def test_changed_model_hash_refuses_before_candidate_construct(tmp_path, monkeypatch, name):
    directory = tmp_path / "models" / name
    directory.mkdir(parents=True)
    payload = b"original"
    asset = directory / "weights"
    asset.write_bytes(b"modified")
    document = {"models": {name: {"path": name, "files": [
        {"path": "weights", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
    ]}}}
    (tmp_path / "models/manifest.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", None)
    monkeypatch.setitem(sys.modules, "voicebar.stt.whisper", None)
    with pytest.raises(SttError, match="checksum failed"):
        load_backend(name, tmp_path, object())


@pytest.fixture
def candidate_cli_root(tmp_path, monkeypatch):
    from voicebar.catalog import Catalog

    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("stt:\n  backend: null\nptt:\n  hotkey: ctrl+alt+f8\n", encoding="utf-8")
    loaded = SimpleNamespace(catalog=Catalog(apps=["Notepad"]))
    monkeypatch.setattr(cli, "load_catalog", lambda *_a, **_k: loaded)
    return tmp_path, loaded


@pytest.mark.parametrize("name", CANDIDATES)
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_wav_cli_accepts_candidate_without_execution(candidate_cli_root, monkeypatch, capsys, name, device):
    from voicebar import stt
    from voicebar.stt import base

    root, loaded = candidate_cli_root
    calls = []

    class FakeBackend:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append("closed")

    def load(actual_name, actual_root, catalog, **options):
        calls.append((actual_name, actual_root, catalog, options))
        return FakeBackend()

    monkeypatch.setattr(stt, "load_backend", load)
    monkeypatch.setattr(base, "transcribe_wav", lambda *_a, **_k: {"text": "open Notepad"})
    monkeypatch.setitem(sys.modules, "voicebar.executor", None)
    assert cli.main(["wav", "saved.wav", "--backend", name, "--device", device, "--dry-run"], project_root=root) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"text": "open Notepad", "plan": [{"action": "open_app", "app": "Notepad"}], "executed": False}
    assert calls == [(name, root, loaded.catalog, {"device": device}), "closed"]


@pytest.mark.parametrize("name", CANDIDATES)
def test_unselected_candidate_refuses_live_ptt_before_capture(candidate_cli_root, monkeypatch, capsys, name):
    root, _ = candidate_cli_root
    monkeypatch.setitem(sys.modules, "voicebar.ptt", None)
    assert cli.main(["listen", "--ptt", "--backend", name], project_root=root) == 2
    assert "accuracy gate is pending" in capsys.readouterr().err


@pytest.mark.parametrize("name", CANDIDATES)
def test_explicit_candidate_dry_run_reaches_fake_ptt(candidate_cli_root, monkeypatch, name):
    root, _ = candidate_cli_root
    calls = []
    monkeypatch.setitem(sys.modules, "voicebar.ptt", SimpleNamespace(listen_ptt=lambda *_a, **k: calls.append(k)))
    assert cli.main(["listen", "--ptt", "--dry-run", "--backend", name, "--device", "cuda"], project_root=root) == 0
    assert calls == [{"backend_name": name, "device": "cuda", "hotkey": "ctrl+alt+f8", "dry_run": True}]


@pytest.mark.parametrize("name", CANDIDATES)
def test_speech_settings_accept_candidate_names(candidate_cli_root, name):
    root, _ = candidate_cli_root
    (root / "config/settings.yaml").write_text(f"stt:\n  backend: {name}\n  device: cuda\n", encoding="utf-8")
    assert cli.speech_settings(root) == ({"backend": name, "device": "cuda"}, {})
