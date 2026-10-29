"""Fully local speech recognition. Downloads are confined to scripts/setup.py."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Backend

BACKEND_NAMES = ("moonshine", "moonshine-medium", "parakeet", "qwen", "qwen-small", "whisper-large", "whisper-turbo")


def model_assets(name: str, device: str) -> tuple[str, ...]:
    from .base import SttError
    if name in ("moonshine", "moonshine-medium"):
        return (name,)
    if name == "parakeet":
        return ("parakeet-cuda" if device == "cuda" else "parakeet", "silero")
    if name in ("qwen", "qwen-small", "whisper-large", "whisper-turbo"):
        return (name, "silero")
    raise SttError(f"Unknown speech backend: {name}")


def load_backend(name: str, root: Path, catalog, device: str = "cpu") -> "Backend":
    from .base import SttError
    from .models import offline_environment, require_model
    offline_environment()
    if name in ("moonshine", "moonshine-medium"):
        if device != "cpu":
            raise SttError("Moonshine 0.1.5 ships a CPU-only execution provider.")
        directory = require_model(root, name, verify_hashes=name == "moonshine-medium")
        from .moonshine import MoonshineBackend
        if name == "moonshine-medium":
            return MoonshineBackend(directory, catalog, name=name)
        return MoonshineBackend(directory, catalog)
    if name == "parakeet":
        if device not in ("cpu", "cuda"):
            raise SttError(f"Unsupported Parakeet device: {device}. Use cpu or cuda.")
        # CUDA uses a distinct, unquantized export with external tensor data.
        # Verify every recorded hash before the model library can resolve files.
        asset = "parakeet-cuda" if device == "cuda" else "parakeet"
        directory = require_model(root, asset, verify_hashes=device == "cuda")
        vad_directory = require_model(root, "silero", verify_hashes=device == "cuda")
        from .parakeet import ParakeetBackend
        return ParakeetBackend(directory, vad_directory, catalog, device=device)
    if name in ("qwen", "qwen-small", "whisper-large", "whisper-turbo"):
        if device not in ("cpu", "cuda"):
            raise SttError(f"Unsupported speech device: {device}. Use cpu or cuda.")
        directory = require_model(root, name, verify_hashes=True)
        vad_directory = require_model(root, "silero", verify_hashes=True)
        if name in ("qwen", "qwen-small"):
            from .qwen import QwenBackend
            backend = QwenBackend(directory, vad_directory, catalog, device=device)
            if name == "qwen-small":
                backend.name = name
            return backend
        from .whisper import WhisperBackend
        return WhisperBackend(directory, vad_directory, catalog, device=device, name=name)
    raise SttError(f"Unknown speech backend: {name}")
