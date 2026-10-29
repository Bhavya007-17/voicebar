"""CUDA configuration and proof validation without loading models or DLLs."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar.stt import load_backend
from voicebar.stt.base import SttError
from voicebar.stt.parakeet import ParakeetBackend, summarize_profile


@pytest.fixture
def fake_runtime(monkeypatch):
    from voicebar.stt import parakeet
    calls = {}

    def load_model(name, directory, **kwargs):
        calls["load"] = (name, directory, kwargs)
        requested = kwargs["providers"][0]
        first = requested[0] if isinstance(requested, tuple) else requested
        session = SimpleNamespace(get_providers=lambda: [first, "CPUExecutionProvider"])
        return SimpleNamespace(asr=SimpleNamespace(_encoder=session, _decoder_joint=session))

    ort = SimpleNamespace(SessionOptions=SimpleNamespace,
                          disable_telemetry_events=lambda: calls.update(telemetry_disabled=True),
                          get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
                          preload_dlls=lambda **kwargs: calls.update(preload=kwargs))
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=load_model))
    monkeypatch.setattr(parakeet, "version", lambda name: "0.12.0")
    monkeypatch.setattr(parakeet, "Silero", lambda directory: SimpleNamespace())
    return ort, calls


def test_cpu_configuration_is_unchanged(fake_runtime, tmp_path):
    _, calls = fake_runtime
    backend = ParakeetBackend(tmp_path, tmp_path, None)
    kwargs = calls["load"][2]
    assert backend.device == "cpu"
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    assert kwargs["quantization"] == "int8"
    assert "preload" not in calls
    assert "preprocessor_config" not in kwargs
    assert kwargs["sess_options"].intra_op_num_threads == 4


def test_cuda_uses_float_local_model_and_cpu_preprocessing(fake_runtime, tmp_path):
    _, calls = fake_runtime
    backend = ParakeetBackend(tmp_path, tmp_path, None, device="cuda")
    kwargs = calls["load"][2]
    assert backend.device == "cuda"
    assert kwargs["quantization"] is None
    assert kwargs["providers"][0][0] == "CUDAExecutionProvider"
    assert kwargs["providers"][1] == "CPUExecutionProvider"
    assert kwargs["preprocessor_config"] == {"use_numpy_preprocessors": True}
    assert calls["preload"] == {"cuda": True, "cudnn": True, "msvc": True, "directory": ""}
    assert set(backend.sessions) == {"encoder", "decoder_joint"}


def test_cuda_rejects_missing_provider_before_model_load(fake_runtime, tmp_path):
    ort, calls = fake_runtime
    ort.get_available_providers = lambda: ["CPUExecutionProvider"]
    with pytest.raises(SttError, match="separate GPU environment"):
        ParakeetBackend(tmp_path, tmp_path, None, device="cuda")
    assert "load" not in calls


def test_cuda_refuses_silent_session_fallback(fake_runtime, tmp_path, monkeypatch):
    _, calls = fake_runtime
    session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(sys.modules["onnx_asr"], "load_model", lambda *args, **kwargs:
                        SimpleNamespace(asr=SimpleNamespace(_encoder=session, _decoder_joint=session)))
    with pytest.raises(SttError, match="CPU fallback refused"):
        ParakeetBackend(tmp_path, tmp_path, None, device="cuda")
    assert "preload" in calls


@pytest.mark.parametrize("device,assets", [("cpu", ["parakeet", "silero"]),
                                          ("cuda", ["parakeet-cuda", "silero"])])
def test_factory_requires_manifest_assets_before_backend(device, assets, monkeypatch, tmp_path):
    from voicebar.stt import models, parakeet
    calls = []
    monkeypatch.setattr(models, "offline_environment", lambda: None)
    monkeypatch.setattr(models, "require_model", lambda root, name, **kwargs:
                        calls.append((name, kwargs["verify_hashes"])) or tmp_path / name)
    monkeypatch.setattr(parakeet, "ParakeetBackend", lambda *args, **kwargs: (args, kwargs))
    result = load_backend("parakeet", tmp_path, None, device=device)
    assert calls == [(name, device == "cuda") for name in assets]
    assert result[1]["device"] == device


def test_factory_missing_cuda_assets_fails_with_no_backend_import(monkeypatch, tmp_path):
    import builtins
    original = builtins.__import__
    imports = []

    def tracked(name, *args, **kwargs):
        imports.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracked)
    with pytest.raises(SttError, match="parakeet-cuda model is unavailable"):
        load_backend("parakeet", tmp_path, None, device="cuda")
    assert "onnx_asr" not in imports
    assert "onnxruntime" not in imports


@pytest.mark.parametrize("name,device", [("moonshine", "cuda"), ("parakeet", "auto")])
def test_unsupported_device_is_explicit(name, device, tmp_path):
    with pytest.raises(SttError, match="CPU-only|Unsupported Parakeet device"):
        load_backend(name, tmp_path, None, device=device)


def test_profile_counts_real_compute_and_does_not_accept_provider_registration_or_copies():
    events = [{"cat": "Session", "args": {"provider": "CUDAExecutionProvider"}},
              {"cat": "Node", "args": {"provider": "CUDAExecutionProvider", "op_name": "MemcpyFromHost"}},
              {"cat": "Node", "args": {"provider": "CPUExecutionProvider", "op_name": "Shape"}}]
    report = summarize_profile(events)
    assert report["cuda_compute_nodes"] == 0
    events.append({"cat": "Node", "args": {"provider": "CUDAExecutionProvider", "op_name": "MatMul"}})
    report = summarize_profile(events)
    assert report["cuda_compute_nodes"] == 1
    assert report["cuda_operations"] == {"MatMul": 1}
    assert report["cpu_operations"] == {"Shape": 1}


def test_collect_profiles_preserves_sessions_when_ort_reuses_filename(tmp_path):
    backend = object.__new__(ParakeetBackend)
    backend.profile_directory = tmp_path
    generated = tmp_path / "ort_timestamp.json"

    def session(operation):
        def end():
            generated.write_text(json.dumps([{"cat": "Node", "args":
                                             {"provider": "CUDAExecutionProvider", "op_name": operation}}]))
            return str(generated)
        return SimpleNamespace(end_profiling=end, get_providers=lambda: ["CUDAExecutionProvider"],
                               get_provider_options=dict)

    backend.sessions = {"encoder": session("Conv"), "decoder_joint": session("LSTM")}
    report = backend.collect_profiles()
    assert report["encoder"]["cuda_operations"] == {"Conv": 1}
    assert report["decoder_joint"]["cuda_operations"] == {"LSTM": 1}
    assert json.loads((tmp_path / "encoder.json").read_text())[0]["args"]["op_name"] == "Conv"


def test_setup_incremental_cuda_preserves_verified_cpu_entries(monkeypatch, tmp_path):
    path = Path(__file__).parents[1] / "scripts/setup.py"
    spec = importlib.util.spec_from_file_location("voicebar_test_setup_cuda", path)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    models = tmp_path / "models"
    models.mkdir()
    entries = {name: {"revision": "pinned-existing", "path": name, "files": [{"path": "old"}]} for name in setup.CPU_MODELS}
    (models / "manifest.json").write_text(json.dumps({"models": entries, "created_utc": "preserve-me"}))
    verified, downloaded = [], []
    monkeypatch.setattr(setup, "require_model", lambda root, name, **kwargs: verified.append((name, kwargs)))
    monkeypatch.setattr(setup, "version", lambda name: "test")

    def download(repo, filename, *, revision, local_dir, token):
        downloaded.append((repo, filename, revision))
        local_dir.mkdir(exist_ok=True)
        (local_dir / filename).write_text("downloaded test fixture")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda **kwargs: SimpleNamespace(model_info=lambda repo: pytest.fail("must reuse CPU revision")),
        hf_hub_download=download))
    setup.setup(tmp_path, cuda=True)
    manifest = json.loads((models / "manifest.json").read_text())
    assert manifest["created_utc"] == "preserve-me"
    assert all(manifest["models"][name] == entries[name] for name in setup.CPU_MODELS)
    assert [item[0] for item in verified] == list(setup.CPU_MODELS)
    assert all(item[1] == {"verify_hashes": True} for item in verified)
    assert [item[1] for item in downloaded] == list(setup.CUDA_FILES)
    assert all(item[2] == "pinned-existing" for item in downloaded)
    assert len(manifest["models"]["parakeet-cuda"]["files"]) == 5
