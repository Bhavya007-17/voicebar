"""Bounded client for an already-running local Ollama server.

No subprocesses, downloads, proxies, redirects or listening sockets.
The public resolver supplies the wall-clock deadline around this client.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import time
from typing import ClassVar


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate server response field")
        result[key] = value
    return result


def _remaining(entered, timeout_s):
    remaining = timeout_s - (time.perf_counter() - entered)
    if remaining <= 0:
        raise TimeoutError("Resolver deadline expired")
    return remaining


class OllamaBackend:
    # Loading depends on num_ctx. Preload and selection must use the same
    # options, or the first spoken command can trigger a cold model reload.
    _OPTIONS: ClassVar[dict[str, int]] = {"temperature": 0, "num_predict": 96, "num_ctx": 8192}

    def __init__(self, model: str, *, port: int = 11434):
        if (not isinstance(model, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*:[\w.-]+", model)
                or "cloud" in model.lower() or ":" not in model):
            raise ValueError("Resolver model must be an explicitly tagged local model")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Resolver port must be from 1 to 65535")
        self.model, self.port = model, port

    def _request(self, path, body=None, *, timeout_s=1.5):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise TimeoutError("Resolver deadline expired")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout_s)
        try:
            encoded = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
            connection.request("GET" if body is None else "POST", path, body=encoded,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"Local model server returned HTTP {response.status}")
            raw = response.read(524289)
            if len(raw) > 524288:
                raise ValueError("Local model response exceeded size limit")
            result = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(result, dict):
                raise TypeError("Local model response must be an object")
            return result
        finally:
            connection.close()

    def local_model(self, timeout_s=1.5):
        """Verify current server metadata before sending any speech to it."""
        info = self._request("/api/show", {"model": self.model}, timeout_s=timeout_s)
        count = info.get("model_info", {}).get("general.parameter_count")
        if (type(count) is not int or not 0 < count <= 4_000_000_000
                or info.get("remote_host") or info.get("remote_model")
                or "completion" not in info.get("capabilities", [])):
            raise ValueError("Model must be a local completion model with at most 4B parameters")
        return info

    def warm(self, timeout_s=60.0):
        entered = time.perf_counter()
        self.local_model(timeout_s)
        result = self._request("/api/generate", {
            "model": self.model, "stream": False, "keep_alive": -1, "think": False,
            "options": dict(self._OPTIONS),
        }, timeout_s=_remaining(entered, timeout_s))
        return result.get("done") is True

    def select(self, request, timeout_s):
        entered = time.perf_counter()
        self.local_model(timeout_s)
        result = self._request("/api/generate", {
            "model": self.model, "prompt": request.prompt,
            "format": request.schema, "stream": False, "keep_alive": -1, "think": False,
            "options": dict(self._OPTIONS),
        }, timeout_s=_remaining(entered, timeout_s))
        if result.get("done") is not True or result.get("done_reason") != "stop":
            return None
        return result.get("response")

    def unload(self, timeout_s=30.0):
        """Release a comparison model; normal runtime keeps its model loaded."""
        result = self._request("/api/generate", {
            "model": self.model, "stream": False, "keep_alive": 0,
        }, timeout_s=timeout_s)
        if result.get("done") is not True:
            raise ValueError("Model unload was not confirmed")
