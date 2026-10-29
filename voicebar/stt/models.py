"""Strictly local, manifest-backed model resolution; never repairs downloads."""

import hashlib
import json
import os
from pathlib import Path

from .base import SttError


def offline_environment() -> None:
    for key in ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_UPDATE_CHECK",
                "DISABLE_TELEMETRY", "DO_NOT_TRACK", "TRANSFORMERS_OFFLINE"):
        os.environ[key] = "1"


def disable_onnx_telemetry() -> None:
    # The installed ONNX Runtime binding documents this as disabling
    # platform-specific collection. Session constructors call this after model
    # preflight, so a missing model does not import its native runtime at all.
    import onnxruntime
    onnxruntime.disable_telemetry_events()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_model(root: Path, name: str, *, verify_hashes: bool = False) -> Path:
    models = (root / "models").resolve()
    try:
        manifest = json.loads((models / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["models"][name]
        directory = (models / entry["path"]).resolve()
        if not directory.is_relative_to(models) or not directory.is_dir():
            raise ValueError("model directory is missing or outside models/")
        if not entry["files"]:
            raise ValueError("empty model manifest")
        for item in entry["files"]:
            path = (directory / item["path"]).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise ValueError(f"missing model file: {item['path']}")
            if path.stat().st_size != item["size"]:
                raise ValueError(f"model file has changed: {item['path']}")
            if verify_hashes and sha256(path) != item["sha256"]:
                raise ValueError(f"model checksum failed: {item['path']}")
        return directory
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SttError(f"{name} model is unavailable ({error}). Run uv run python scripts/setup.py.") from error
