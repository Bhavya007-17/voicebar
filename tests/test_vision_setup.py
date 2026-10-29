"""Vision downloads are explicit setup operations against a reachable local server."""

import importlib.util
import json
from pathlib import Path

import pytest

from voicebar.resolver.ollama import OllamaBackend


@pytest.fixture
def setup_module():
    source = Path(__file__).parents[1] / "scripts/setup.py"
    spec = importlib.util.spec_from_file_location("voicebar_vision_setup_tests", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("resolver:\n  port: 12345\n", encoding="utf-8")
    return tmp_path


def fake_server(monkeypatch, setup_module, *, version="0.30.10", vision=True, inventory=True):
    requests, pulls = [], []
    monkeypatch.setattr(setup_module.shutil, "which", lambda name: "C:/fake/ollama.exe")

    def request(client, path, body=None, **kwargs):
        requests.append((path, client.port))
        if path == "/api/version":
            return {"version": version}
        if path == "/api/tags":
            return {"models": [{"name": setup_module.VISION_MODEL, "digest": "test-digest"}] if inventory else []}
        raise AssertionError(path)

    monkeypatch.setattr(OllamaBackend, "_request", request)
    monkeypatch.setattr(OllamaBackend, "local_model", lambda *args, **kwargs:
                        {"capabilities": ["completion", *(["vision"] if vision else [])]})
    monkeypatch.setattr(setup_module.subprocess, "run", lambda command, **kwargs: pulls.append((command, kwargs)))
    return requests, pulls


def test_explicit_vision_setup_uses_loopback_and_records_local_inventory(setup_module, configured_root, monkeypatch):
    requests, pulls = fake_server(monkeypatch, setup_module)
    setup_module.setup_vision(configured_root)
    assert requests == [("/api/version", 12345), ("/api/tags", 12345)]
    command, options = pulls[0]
    assert command == ["C:/fake/ollama.exe", "pull", "qwen3-vl:2b-instruct"]
    assert options["shell"] is False and options["check"] is True
    assert options["env"]["OLLAMA_HOST"] == "127.0.0.1:12345"
    assert options["env"]["OLLAMA_NO_CLOUD"] == "1"
    manifest = json.loads((configured_root / "models/vision-manifest.json").read_text())
    assert manifest["models"] == [{"name": setup_module.VISION_MODEL, "digest": "test-digest"}]
    assert manifest["server_version"] == "0.30.10"


@pytest.mark.parametrize("version", ["0.12.6", "0.11.100", "", "not-a-version"])
def test_old_or_unknown_server_refused_before_download(setup_module, configured_root, monkeypatch, version):
    _, pulls = fake_server(monkeypatch, setup_module, version=version)
    with pytest.raises(ValueError, match="requires Ollama"):
        setup_module.setup_vision(configured_root)
    assert pulls == []


def test_absent_server_does_not_auto_start_or_download(setup_module, configured_root, monkeypatch):
    _, pulls = fake_server(monkeypatch, setup_module)

    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError("test server absent")

    monkeypatch.setattr(OllamaBackend, "_request", unavailable)
    with pytest.raises(ConnectionRefusedError):
        setup_module.setup_vision(configured_root)
    assert pulls == []


@pytest.mark.parametrize("model", ["gemma4:31b-cloud", "qwen3-vl:8b", "qwen3-vl:2b-thinking"])
def test_unverified_model_refused_before_server_contact(setup_module, configured_root, monkeypatch, model):
    requests, pulls = fake_server(monkeypatch, setup_module)
    (configured_root / "config/settings.yaml").write_text(f"vision:\n  model: {model}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="supports only the verified"):
        setup_module.setup_vision(configured_root)
    assert requests == pulls == []


@pytest.mark.parametrize("vision,inventory", [(False, True), (True, False)])
def test_invalid_download_does_not_write_manifest(setup_module, configured_root, monkeypatch, vision, inventory):
    fake_server(monkeypatch, setup_module, vision=vision, inventory=inventory)
    with pytest.raises(ValueError):
        setup_module.setup_vision(configured_root)
    assert not (configured_root / "models/vision-manifest.json").exists()
