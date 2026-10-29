"""Explicit one-time model downloads. Runtime never downloads models."""

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from voicebar.stt.models import require_model, sha256


def file_manifest(directory):
    return [
        {"path": path.relative_to(directory).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(directory.rglob("*"))
        if path.is_file() and ".cache" not in path.relative_to(directory).parts
    ]


CPU_MODELS = ("moonshine", "parakeet", "silero")
PARAKEET_REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
CUDA_FILES = ("config.json", "vocab.txt", "encoder-model.onnx", "encoder-model.onnx.data",
              "decoder_joint-model.onnx")
ADDITIONAL_MODELS = {
    "qwen-small": ("Qwen/Qwen3-ASR-0.6B-hf", "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c", "Apache-2.0",
                   ("model.safetensors", "config.json", "generation_config.json", "processor_config.json",
                    "chat_template.jinja", "tokenizer.json", "tokenizer_config.json")),
    "qwen": ("Qwen/Qwen3-ASR-1.7B-hf", "bcd2b5b7f32b480ab5790554cfa8347f246a14f3", "Apache-2.0",
             ("model.safetensors", "config.json", "generation_config.json", "processor_config.json",
              "chat_template.jinja", "tokenizer.json", "tokenizer_config.json")),
    "whisper-large": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478", "MIT",
                      ("config.json", "model.bin", "tokenizer.json", "preprocessor_config.json", "vocabulary.json")),
    "whisper-turbo": ("dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", "MIT",
                      ("config.json", "model.bin", "tokenizer.json", "preprocessor_config.json", "vocabulary.json")),
}
MOONSHINE_MEDIUM_URL = "https://download.moonshine.ai/model/medium-streaming-en/quantized_26_08_21"
CANDIDATE_NAMES = (*ADDITIONAL_MODELS, "moonshine-medium")
VISION_MODEL = "qwen3-vl:2b-instruct"
VISION_MIN_OLLAMA = (0, 12, 7)


def setup_resolver(root: Path):
    """Download only the local candidates named in config, via installed Ollama."""
    from voicebar.catalog import _yaml_file
    from voicebar.resolver.ollama import OllamaBackend

    config = _yaml_file(root / "config/settings.yaml").get("resolver", {})
    candidates = config.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Configure resolver.candidates before downloading")
    executable = shutil.which("ollama")
    if executable is None:
        installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        if not installed.is_file():
            raise RuntimeError("Ollama is not installed; Stage 6 stops here")
        executable = str(installed)
    # Reachability is checked first: the CLI must not auto-start a server.
    probe = OllamaBackend(candidates[0], port=config.get("port", 11434))
    probe._request("/api/version", timeout_s=3)
    environment = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{probe.port}")
    for model in candidates:
        client = OllamaBackend(model, port=probe.port)
        print(f"Downloading configured local resolver candidate {model}...", flush=True)
        subprocess.run([executable, "pull", model], env=environment, shell=False,
                       check=True, timeout=1800)
        client.local_model(timeout_s=10)  # Refuse a tag that exceeds 4B or is remote.
    inventory = probe._request("/api/tags", timeout_s=10)
    selected = [row for row in inventory.get("models", []) if row.get("name") in candidates]
    directory = root / "models"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "resolver-manifest.json").write_text(json.dumps({
        "observed_utc": datetime.now(UTC).isoformat(), "models": selected,
    }, indent=2) + "\n", encoding="utf-8")
    print("Resolver candidates are local; runtime has no pull/download API.")


def setup_vision(root: Path):
    """Explicitly download the verified small local vision selector, never at runtime."""
    from voicebar.catalog import _yaml_file
    from voicebar.resolver.ollama import OllamaBackend

    settings = _yaml_file(root / "config/settings.yaml")
    config = settings.get("vision", {})
    if not isinstance(config, dict):
        raise TypeError("vision must be a settings mapping")
    model = config.get("model", VISION_MODEL)
    if model != VISION_MODEL:
        raise ValueError(f"Vision setup currently supports only the verified {VISION_MODEL} model")
    resolver = settings.get("resolver", {})
    if not isinstance(resolver, dict):
        raise TypeError("resolver must be a settings mapping")
    client = OllamaBackend(model, port=resolver.get("port", 11434))
    executable = shutil.which("ollama")
    if executable is None:
        installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        if not installed.is_file():
            raise RuntimeError("Ollama is not installed; vision setup cannot continue")
        executable = str(installed)
    # A reachable server is required, so `pull` cannot auto-start one using a
    # different model directory or bind address. Only explicit setup downloads.
    server = client._request("/api/version", timeout_s=3)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", str(server.get("version", "")))
    if match is None or tuple(map(int, match.groups())) < VISION_MIN_OLLAMA:
        raise ValueError("The verified vision model requires Ollama 0.12.7 or newer")
    environment = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{client.port}", OLLAMA_NO_CLOUD="1")
    print(f"Downloading local vision selector {model} (approximately 1.9 GB)...", flush=True)
    subprocess.run([executable, "pull", model], env=environment, shell=False,
                   check=True, timeout=1800)
    info = client.local_model(timeout_s=10)
    if "vision" not in info.get("capabilities", []):
        raise ValueError("The downloaded local model does not expose vision capability")
    inventory = client._request("/api/tags", timeout_s=10)
    selected = [row for row in inventory.get("models", []) if row.get("name") == model]
    if len(selected) != 1:
        raise ValueError("The downloaded vision model is absent or ambiguous in the local inventory")
    directory = root / "models"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vision-manifest.json").write_text(json.dumps({
        "observed_utc": datetime.now(UTC).isoformat(),
        "source": "https://ollama.com/library/qwen3-vl:2b-instruct",
        "server_version": server["version"], "models": selected,
    }, indent=2) + "\n", encoding="utf-8")
    print("Vision model is local; runtime has no pull/download API.", flush=True)


def serve_resolver(root: Path):
    """Explicit setup utility, never called by the voice-command runtime."""
    from voicebar.catalog import _yaml_file
    config = _yaml_file(root / "config/settings.yaml").get("resolver", {})
    portable = root / "models/ollama-runtime-0.40.11/ollama.exe"
    portable_manifest = portable.parent / "voicebar-runtime.json"
    executable = str(portable) if portable.is_file() and portable_manifest.is_file() else (
        shutil.which("ollama") or str(
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"))
    if not Path(executable).is_file():
        raise RuntimeError("Ollama is not installed; Stage 6 stops here")
    port = config.get("port", 11434)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Resolver port must be from 1 to 65535")
    environment = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}",
                       OLLAMA_MODELS=str(root / "models/ollama"), OLLAMA_NO_CLOUD="1")
    subprocess.run([executable, "serve"], env=environment, shell=False, check=True)


def setup(root: Path, *, cuda: bool = False, candidates=()) -> None:
    models = root / "models"
    manifest_path = models / "manifest.json"
    if any(name not in CANDIDATE_NAMES for name in candidates):
        raise ValueError("Unknown additional speech candidate.")
    requested = (*CPU_MODELS, *(("parakeet-cuda",) if cuda else ()), *dict.fromkeys(candidates))
    manifest = {}
    entries = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["models"]
        if not isinstance(entries, dict):
            raise TypeError("Model manifest models must be an object.")
        # Verify existing assets before extending the manifest. Never bless a
        # modified local file by silently calculating a new checksum for it.
        for name in entries:
            require_model(root, name, verify_hashes=True)
    missing = [name for name in requested if name not in entries]
    if not missing:
        print("All local model files match their SHA-256 manifest; nothing downloaded.")
        return
    # Explicit setup can use the network; runtime always overwrites these switches.
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HF_HOME"] = str(models / ".hf-cache")
    models.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import HfApi, hf_hub_download

    if "moonshine" in missing:
        from moonshine_voice import ModelArch
        from moonshine_voice.download import get_model_for_language
        print("Downloading Moonshine Small Streaming English...", flush=True)
        moon, arch = get_model_for_language("en", ModelArch.SMALL_STREAMING, cache_root=models / "moonshine")
        moon = Path(moon).resolve()
        entries["moonshine"] = {
            "path": moon.relative_to(models.resolve()).as_posix(), "architecture": arch.name,
            "source": "moonshine-voice 0.1.5 native dependency catalog", "files": file_manifest(moon),
        }
    if "moonshine-medium" in missing:
        from moonshine_voice import ModelArch
        from moonshine_voice.download import download_model_from_info, find_model_info
        # The native dependency catalog supplies filenames and validates its
        # downloads. Reject catalog drift before requesting any model bytes.
        package_version = version("moonshine-voice")
        if package_version != "0.1.5":
            raise ValueError("Moonshine Medium setup requires the verified moonshine-voice 0.1.5 catalog.")
        info = find_model_info("en", ModelArch.MEDIUM_STREAMING)
        if info["download_url"] != MOONSHINE_MEDIUM_URL:
            raise ValueError("Moonshine Medium catalog URL changed; verify the new model before downloading.")
        if info["model_arch"] != ModelArch.MEDIUM_STREAMING or info["language"] != "en":
            raise ValueError("Moonshine Medium catalog returned a different architecture or language.")
        print("Downloading Moonshine Medium Streaming English...", flush=True)
        # This helper downloads only the verified STT group, without the
        # get_model_for_language helper's unused optional spelling prefetch.
        moon, arch = download_model_from_info(info, cache_root=models / "moonshine-medium")
        if arch != ModelArch.MEDIUM_STREAMING:
            raise ValueError("Moonshine Medium downloader returned a different architecture.")
        moon = Path(moon).resolve()
        entries["moonshine-medium"] = {
            "path": moon.relative_to(models.resolve()).as_posix(), "architecture": arch.name,
            "source": MOONSHINE_MEDIUM_URL, "revision": "quantized_26_08_21", "license": "MIT",
            "catalog_package": f"moonshine-voice {package_version}", "files": file_manifest(moon),
        }
    api = HfApi(token=False)
    for name, repo, filenames, license_name in (
        ("parakeet", PARAKEET_REPO,
         ("config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"), "CC-BY-4.0"),
        ("silero", "istupakov/silero-vad-onnx", ("silero_vad.onnx",), "MIT"),
        ("parakeet-cuda", PARAKEET_REPO, CUDA_FILES, "CC-BY-4.0"),
    ):
        if name not in missing:
            continue
        # Compare CPU and CUDA exports from the same resolved repository commit.
        revision = (entries.get("parakeet", {}).get("revision") if name == "parakeet-cuda" else None)
        revision = revision or api.model_info(repo).sha
        directory = models / name
        for filename in filenames:
            print(f"Downloading {name}/{filename} at {revision}...", flush=True)
            hf_hub_download(repo, filename, revision=revision, local_dir=directory, token=False)
        entries[name] = {
            "path": name, "source": f"https://huggingface.co/{repo}", "revision": revision,
            "license": license_name, "files": file_manifest(directory),
        }
    for name in candidates:
        if name not in missing or name not in ADDITIONAL_MODELS:
            continue
        repo, revision, license_name, filenames = ADDITIONAL_MODELS[name]
        directory = models / name
        for filename in filenames:
            print(f"Downloading {name}/{filename} at {revision}...", flush=True)
            hf_hub_download(repo, filename, revision=revision, local_dir=directory, token=False)
        entries[name] = {
            "path": name, "source": f"https://huggingface.co/{repo}", "revision": revision,
            "license": license_name, "files": file_manifest(directory),
        }
    packages = dict(manifest.get("packages", {}))
    for name in ("moonshine-voice", "onnx-asr", "onnxruntime", "onnxruntime-gpu", "torch",
                 "transformers", "faster-whisper", "ctranslate2", "accelerate"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            pass
    manifest.update({"version": 1, "models": entries, "packages": packages})
    manifest.setdefault("created_utc", datetime.now(UTC).isoformat())
    manifest["updated_utc"] = datetime.now(UTC).isoformat()
    temporary = models / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(models / "manifest.json")
    print("Models saved and hashed. Runtime loading is local only.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cuda", action="store_true", help="also download the unquantized local Parakeet CUDA assets")
    parser.add_argument("--candidate", choices=CANDIDATE_NAMES, action="append", default=[],
                        help="also download a pinned accuracy candidate; repeat for multiple models")
    parser.add_argument("--resolver-only", action="store_true",
                        help="download configured resolver candidates using an already-running local Ollama")
    parser.add_argument("--serve-resolver", action="store_true",
                        help="explicitly run the local-only Ollama server using project model files")
    parser.add_argument("--vision", action="store_true",
                        help="download the verified local vision model using an already-running local Ollama")
    args = parser.parse_args()
    if sum((args.serve_resolver, args.resolver_only, args.vision)) > 1:
        parser.error("choose only one of --serve-resolver, --resolver-only, or --vision")
    if args.serve_resolver:
        serve_resolver(args.root.resolve())
    elif args.vision:
        setup_vision(args.root.resolve())
    elif args.resolver_only:
        setup_resolver(args.root.resolve())
    else:
        setup(args.root.resolve(), cuda=args.cuda, candidates=args.candidate)
