"""First-run defaults and validated snapshots for live configuration reloads."""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .catalog import CatalogError, LoadedCatalog, _yaml_file, load_catalog

DEFAULT_SETTINGS = {
    "name": "computer", "names": ["computer"], "mode": "name", "followup_seconds": 8,
    "stt": {"backend": None, "device": "cpu"},
    "ptt": {"hotkey": "ctrl+alt+f8"},
    "ui": {"monitor": "primary", "dwell_ms": 450, "idle_ms": 4000,
           "toggle_listening": "ctrl+alt+f9", "toggle_bar": "ctrl+alt+f10",
           "hold_to_talk": "ctrl+alt+f8", "execution_timeout_seconds": 10,
           "copilot_key": True},
    "logs": {"max_file_bytes": 2097152, "max_total_bytes": 67108864,
             "keep_transcripts": True},
    "audio": {"silence_seconds": 0.8, "short_silence_seconds": 0.25,
              "adaptive_endpoint": False},
    "resolver": {"backend": "ollama", "model": None, "timeout_ms": 1500, "port": 11434},
    "executor": {"window_timeout_seconds": 30, "clipboard_threshold": 256,
                 "new_window_apps": [], "browser_apps": [], "window_executables": {}},
    "engines": {"google": "https://www.google.com/search?q={q}"},
}


def ensure_default_config(root, *, portable=False) -> list[Path]:
    """Create absent configuration, or explicitly back up a machine-specific set."""
    root = Path(root).resolve()
    directory = root / "config"
    if directory.is_symlink():
        raise CatalogError("Refusing to initialize config through a symbolic link")
    if portable and directory.exists():
        backup = root / ".cache/config-backups" / (
            f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}")
        if not directory.resolve().is_relative_to(root) or not backup.resolve().is_relative_to(root):
            raise CatalogError("Config backup must stay inside the project")
        backup.parent.mkdir(parents=True, exist_ok=True)
        directory.replace(backup)
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for name, value in {"settings.yaml": DEFAULT_SETTINGS, "aliases.yaml": {},
                        "sites.yaml": {}, "pinned.yaml": []}.items():
        path = directory / name
        try:
            with path.open("x", encoding="utf-8") as stream:
                yaml.safe_dump(value, stream, sort_keys=False, allow_unicode=True)
        except FileExistsError:
            continue
        created.append(path)
    (directory / "apps").mkdir(exist_ok=True)
    return created


def first_run_message(created) -> str:
    if not created:
        return ""
    return ("Created default config. Listening starts Off. Choose and test a local speech backend; "
            "run 'voicebar apps' and put the exact apps you want in config/pinned.yaml before "
            "using open mode.")


def _fingerprint(root: Path) -> tuple:
    directory = root / "config"
    return tuple((path.relative_to(directory).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in sorted(directory.rglob("*"))
                 if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"})


def validate_configuration(root, loaded=None) -> tuple[LoadedCatalog, dict]:
    """Validate every runtime setting without loading models, sockets or audio."""
    from .endpoint import EndpointSettings
    from .executor.common import load_settings
    from .listening import configured_gate
    from .logs import load_log_settings
    from .resolver.base import Resolver
    from .resolver.ollama import OllamaBackend
    from .screen import vision_settings
    from .stt import BACKEND_NAMES
    from .ui.settings import load_ui_settings

    root = Path(root)
    loaded = loaded if loaded is not None else load_catalog(root)
    settings = _yaml_file(root / "config/settings.yaml")
    if not isinstance(settings, dict):
        raise CatalogError("settings.yaml must be a mapping")
    stt = settings.get("stt", {})
    if not isinstance(stt, dict):
        raise CatalogError("settings.yaml stt must be a mapping")
    if stt.get("backend") not in (None, *BACKEND_NAMES):
        raise CatalogError("stt backend must be a supported local backend or null")
    if stt.get("device", "cpu") not in {"cpu", "cuda"}:
        raise CatalogError("stt device must be cpu or cuda")
    resolver = settings.get("resolver", {})
    if not isinstance(resolver, dict):
        raise CatalogError("settings.yaml resolver must be a mapping")
    if resolver.get("model") is not None:
        if resolver.get("backend", "ollama") != "ollama":
            raise CatalogError("resolver backend must be ollama")
        backend = OllamaBackend(resolver["model"], port=resolver.get("port", 11434))
        Resolver(backend, timeout_ms=resolver.get("timeout_ms", 1500))
    configured_gate(root, loaded, document=settings)
    vision_settings(settings)
    load_ui_settings(root, document=settings)
    load_settings(root, loaded, document=settings)
    load_log_settings(root, document=settings)
    EndpointSettings.from_document(settings)
    return loaded, settings


@dataclass(frozen=True)
class ReloadResult:
    loaded: LoadedCatalog | None
    settings: dict | None
    changed_files: tuple[str, ...]
    error: str | None = None
    executor_settings: dict | None = None

    @property
    def ok(self):
        return self.error is None


class ConfigReloader:
    """Poll on a worker; publish only a complete, validated config generation."""

    def __init__(self, root, loaded=None):
        from .executor.common import load_settings

        self.root = Path(root)
        self.loaded, self.settings = validate_configuration(self.root, loaded)
        self.executor_settings = load_settings(self.root, self.loaded, document=self.settings)
        self._signature = _fingerprint(self.root)
        self._lock = threading.Lock()

    def poll(self) -> ReloadResult | None:
        from .executor.common import load_settings

        with self._lock:
            try:
                signature = _fingerprint(self.root)
            except OSError as error:
                return ReloadResult(None, None, (), f"Cannot read config: {error}")
            if signature == self._signature:
                return None
            before, after = dict(self._signature), dict(signature)
            changed = tuple(sorted(name for name in before.keys() | after.keys()
                                   if before.get(name) != after.get(name)))
            self._signature = signature
            try:
                loaded, settings = validate_configuration(self.root)
                if _fingerprint(self.root) != signature:
                    # A save raced validation. Retry the stable file generation next poll.
                    self._signature = ()
                    return ReloadResult(None, None, changed, "Config changed while loading; retrying")
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                return ReloadResult(None, None, changed, f"Config was not reloaded: {error}")
            executor_settings = load_settings(self.root, loaded, document=settings)
            self.loaded, self.settings, self.executor_settings = loaded, settings, executor_settings
            return ReloadResult(loaded, settings, changed, executor_settings=executor_settings)
