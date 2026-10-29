"""The smaller Qwen checkpoint is an explicit, hash-checked offline candidate."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar.stt import BACKEND_NAMES, load_backend, model_assets
from voicebar.stt.base import SttError


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_small_qwen_reuses_offline_qwen_backend_with_distinct_identity(tmp_path, monkeypatch, device):
    from voicebar.stt import models

    checked, constructed = [], []
    catalog = object()
    backend = SimpleNamespace(name="qwen")

    def require(root, name, **options):
        checked.append((name, options))
        return root / "models" / name

    def build(directory, vad, actual_catalog, **options):
        constructed.append((directory, vad, actual_catalog, options))
        return backend

    monkeypatch.setattr(models, "require_model", require)
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", SimpleNamespace(QwenBackend=build))
    assert "qwen-small" in BACKEND_NAMES
    assert model_assets("qwen-small", device) == ("qwen-small", "silero")
    assert load_backend("qwen-small", tmp_path, catalog, device=device) is backend
    assert checked == [("qwen-small", {"verify_hashes": True}), ("silero", {"verify_hashes": True})]
    assert constructed == [(tmp_path / "models/qwen-small", tmp_path / "models/silero", catalog, {"device": device})]
    assert backend.name == "qwen-small"


def test_small_qwen_missing_files_never_imports_model_library(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "voicebar.stt.qwen", None)
    with pytest.raises(SttError, match="model is unavailable"):
        load_backend("qwen-small", tmp_path, object(), device="cuda")


def test_small_qwen_rejects_unknown_device_before_reading_models(tmp_path, monkeypatch):
    from voicebar.stt import models

    monkeypatch.setattr(models, "require_model", lambda *args, **kwargs: pytest.fail("No model read expected"))
    with pytest.raises(SttError, match="Use cpu or cuda"):
        load_backend("qwen-small", tmp_path, object(), device="auto")


def test_setup_downloads_only_pinned_small_qwen_files_and_preserves_existing_models(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "scripts/setup.py"
    spec = importlib.util.spec_from_file_location("voicebar_qwen_small_setup", source)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    models = tmp_path / "models"
    models.mkdir()
    existing = {name: {"path": name, "files": [{"path": "unchanged"}]} for name in setup.CPU_MODELS}
    (models / "manifest.json").write_text(json.dumps({"models": existing, "created_utc": "keep"}))
    verified, downloaded = [], []
    monkeypatch.setattr(setup, "require_model", lambda root, name, **kwargs: verified.append((name, kwargs)))
    monkeypatch.setattr(setup, "version", lambda name: "test")

    def download(repo, filename, *, revision, local_dir, token):
        downloaded.append((repo, filename, revision, token))
        local_dir.mkdir(exist_ok=True)
        (local_dir / filename).write_text("test fixture")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda **kwargs: SimpleNamespace(model_info=lambda *args: pytest.fail("Revision is already pinned")),
        hf_hub_download=download))
    setup.setup(tmp_path, candidates=("qwen-small",))
    result = json.loads((models / "manifest.json").read_text())
    assert result["created_utc"] == "keep"
    assert all(result["models"][name] == entry for name, entry in existing.items())
    assert verified == [(name, {"verify_hashes": True}) for name in setup.CPU_MODELS]
    assert len(downloaded) == 7
    assert {item[0] for item in downloaded} == {"Qwen/Qwen3-ASR-0.6B-hf"}
    assert {item[2] for item in downloaded} == {"7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c"}
    assert {item[3] for item in downloaded} == {False}
    entry = result["models"]["qwen-small"]
    assert entry["path"] == "qwen-small" and len(entry["files"]) == 7
    assert all(len(item["sha256"]) == 64 for item in entry["files"])
