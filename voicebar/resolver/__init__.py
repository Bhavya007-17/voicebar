"""Optional local fallback; an offline service fails closed."""

from __future__ import annotations

import math
import threading
import time


def resolve(text, catalog, context=None):
    """Unconfigured fallback does nothing; runtime uses configured_resolver."""
    return


def release_configured_models(document, *, timeout_s=10.0, cancelled=None):
    """Unload only configured resident tags, sharing one preparation deadline."""
    from .ollama import OllamaBackend

    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("Selector preparation needs a finite positive deadline")
    deadline = time.monotonic() + timeout_s

    def remaining():
        if cancelled is not None and cancelled():
            raise RuntimeError("Selector preparation was cancelled")
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Selector release deadline expired")
        return value

    resolver, vision = document.get("resolver", {}), document.get("vision", {})
    configured = []
    if resolver.get("model") is not None:
        if resolver.get("backend", "ollama") != "ollama":
            raise ValueError("Only local Ollama selectors can be released")
        configured.append((resolver.get("port", 11434), resolver["model"]))
    if vision.get("enabled", False):
        configured.append((resolver.get("port", 11434), vision.get("model", "qwen3-vl:2b-instruct")))
    clients = {(port, model): OllamaBackend(model, port=port) for port, model in configured}
    released = []
    for port in sorted({port for port, _ in clients}):
        selected = {model: client for (client_port, model), client in clients.items() if client_port == port}
        client = next(iter(selected.values()))

        def names(client=client):
            rows = client._request("/api/ps", timeout_s=remaining()).get("models")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                                                 for row in rows):
                raise ValueError("Invalid local model residency metadata")
            return {row["name"] for row in rows}

        try:
            resident = names()
        except ConnectionRefusedError:
            continue  # Optional server is not running; speech remains available.
        for model in sorted(resident & selected.keys()):
            selected[model].unload(timeout_s=remaining())
            released.append({"port": port, "model": model})
        if names() & selected.keys():
            raise RuntimeError("Configured selector remained resident after unload")
    remaining()
    return released


class _ManagedResolver:
    """Callable fallback whose old preload/selection can be quiesced on reload."""

    def __init__(self, backend, resolver, *, automatic=True):
        self.backend, self.resolver = backend, resolver
        self.automatic = automatic
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.loading = threading.Lock()
        self.dispatch = threading.Lock()

    def stop(self):
        self.stopped.set()
        self.ready.clear()

    def wait_idle(self, timeout_s):
        deadline = time.monotonic() + timeout_s
        for lock in (self.loading, self.dispatch):
            if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return False
            lock.release()
        return self.resolver.wait_idle(max(0.0, deadline - time.monotonic()))

    def _warm(self):
        try:
            if self.stopped.is_set():
                return False
            self.automatic = True
            if self.backend.warm() is True and not self.stopped.is_set():
                self.resolver.warm_error = None
                self.ready.set()
                return True
            return False
        except Exception as error:  # noqa: BLE001 - optional preload must not disable tier 1.
            self.resolver.warm_error = type(error).__name__
            return False
        finally:
            self.loading.release()

    def _claim(self):
        return not self.stopped.is_set() and self.loading.acquire(blocking=False)

    def warm(self):
        """Prepare synchronously on the controller's ordered preload worker."""
        return self._warm() if self._claim() else False

    def start_preload(self):
        if not self._claim():
            return
        try:
            threading.Thread(target=self._warm, name="voicebar-resolver-preload", daemon=True).start()
        except Exception:  # noqa: BLE001 - failed optional preload stays unavailable.
            self.loading.release()

    def __call__(self, text, catalog, context=None):
        if self.stopped.is_set():
            return None
        if not self.ready.is_set():
            if self.automatic:
                self.start_preload()
            return None
        if not self.dispatch.acquire(blocking=False):
            return None
        try:
            if self.stopped.is_set():
                return None
            plan = self.resolver.resolve(text, catalog, context)
            if self.resolver.last_outcome == "backend_error":
                self.ready.clear()
            return None if self.stopped.is_set() else plan
        finally:
            self.dispatch.release()


def configured_resolver(root, *, warm=True, document=None, preload=True):
    from ..catalog import CatalogError, _yaml_file
    from .base import Resolver
    from .ollama import OllamaBackend

    settings = _yaml_file(root / "config/settings.yaml") if document is None else document
    config = settings.get("resolver", {})
    if not isinstance(config, dict):
        raise CatalogError("settings.yaml resolver must be a mapping")
    if config.get("model") is None:
        return resolve
    if config.get("backend", "ollama") != "ollama":
        raise ValueError("Only the local Ollama resolver is configured")
    backend = OllamaBackend(config["model"], port=config.get("port", 11434))
    resolver = Resolver(backend, timeout_ms=config.get("timeout_ms", 1500))
    if not warm:
        return resolver.resolve

    managed = _ManagedResolver(backend, resolver, automatic=preload)
    if preload:
        managed.start_preload()
    return managed
