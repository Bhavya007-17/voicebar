"""Bounded local JSON-lines decisions with optional transcript removal."""

from __future__ import annotations

import heapq
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .catalog import CatalogError, _yaml_file

_LOCK = threading.RLock()
_PRIVATE_FIELDS = {
    "text", "transcript", "transcripts", "utterance", "query", "prompt", "payload",
    "detail", "message", "error", "exception", "candidates", "alternatives", "url",
    "heard", "partial", "partials", "speech", "raw", "normalized", "phrase",
}


def load_log_settings(root: Path, document=None) -> dict:
    path = Path(root) / "config/settings.yaml"
    if document is None:
        document = _yaml_file(path) if path.exists() else {}
    supplied = document.get("logs", {}) if isinstance(document, dict) else None
    if not isinstance(supplied, dict):
        raise CatalogError("settings.yaml logs must be a mapping")
    defaults = {"max_file_bytes": 2 * 1024 * 1024,
                "max_total_bytes": 64 * 1024 * 1024, "keep_transcripts": True}
    if set(supplied) - defaults.keys():
        raise CatalogError(f"Unknown logs settings: {sorted(set(supplied) - defaults.keys())}")
    result = {**defaults, **supplied}
    for key in ("max_file_bytes", "max_total_bytes"):
        if type(result[key]) is not int or not 1024 <= result[key] <= 1024 * 1024 * 1024:
            raise CatalogError(f"logs {key} must be an integer from 1024 to 1073741824")
    if result["max_file_bytes"] > result["max_total_bytes"]:
        raise CatalogError("logs max_file_bytes must not exceed max_total_bytes")
    if type(result["keep_transcripts"]) is not bool:
        raise CatalogError("logs keep_transcripts must be true or false")
    return result


def private_decision(value):
    """Copy decisions without speech, typed/search payloads or diagnostic echoes."""
    if isinstance(value, dict):
        return {key: private_decision(item) for key, item in value.items()
                if key.casefold() not in _PRIVATE_FIELDS
                and not key.casefold().endswith(("_text", "_transcript", "_query", "_prompt", "_error"))}
    if isinstance(value, (list, tuple)):
        return [private_decision(item) for item in value]
    return value


def _files(directory: Path) -> list[Path]:
    # Logs and reports live directly in logs/. Never follow links or remove directories.
    return sorted((path for path in directory.iterdir()
                   if path.is_file() and not path.is_symlink()),
                  key=lambda path: (path.stat().st_mtime_ns, path.name))


def cap_logs(directory: Path, maximum: int, *, active: Path | None = None) -> None:
    files = _files(directory)
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= maximum:
            break
        if path == active:
            continue
        size = path.stat().st_size
        path.unlink()
        total -= size


class DecisionLog:
    """Append/rotate one writer; policy is reloaded on the next log write."""

    def __init__(self, root, *, prefix="listen", path=None):
        self.root = Path(root)
        self.directory = self.root / "logs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = Path(path) if path is not None else self.directory / (
            f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.jsonl")
        if self.path.parent.resolve() != self.directory.resolve():
            raise ValueError("Decision log must be directly inside this project's logs folder")
        self.lock = _LOCK
        self._policy_signature = None
        self._settings = load_log_settings(self.root)
        self._part = 0
        with self.lock:
            cap_logs(self.directory, self._settings["max_total_bytes"], active=self.path)

    def _policy(self):
        path = self.root / "config/settings.yaml"
        signature = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        if signature != self._policy_signature:
            # A malformed replacement cannot silently turn transcript storage back on.
            try:
                self._settings = load_log_settings(self.root)
            except CatalogError:
                # The config reloader reports the error. A broken save cannot expose speech.
                self._settings = {**self._settings, "keep_transcripts": False}
            self._policy_signature = signature
        return self._settings

    def __call__(self, row):
        with self.lock:
            settings = self._policy()
            record = {"time": datetime.now(UTC).isoformat(), **row}
            if not settings["keep_transcripts"]:
                record = {**private_decision(record), "transcripts_stored": False}
            encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > settings["max_file_bytes"]:
                # Keep the decision and numeric timings even when a huge payload cannot fit.
                record = {**private_decision(record), "transcripts_stored": False,
                          "log_payload_dropped": True}
                encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                if len(encoded) > settings["max_file_bytes"]:
                    encoded = (json.dumps({"time": record["time"], "event": "log_record_too_large",
                                           "original_bytes": len(encoded)}) + "\n").encode("utf-8")
            if self.path.exists() and self.path.stat().st_size + len(encoded) > settings["max_file_bytes"]:
                self._part += 1
                archive = self.path.with_name(f"{self.path.stem}-part{self._part}-{uuid.uuid4().hex[:6]}.jsonl")
                self.path.replace(archive)
            with self.path.open("ab") as stream:
                stream.write(encoded)
            cap_logs(self.directory, settings["max_total_bytes"], active=self.path)


def read_recent(root, last=50) -> list[dict]:
    """Read newest complete records; a concurrently written tail is ignored."""
    if type(last) is not int or not 1 <= last <= 10000:
        raise ValueError("logs --last must be an integer from 1 to 10000")
    directory = Path(root) / "logs"
    if not directory.exists():
        return []
    rows = []
    sequence = 0
    with _LOCK:
        for path in _files(directory):
            if path.suffix != ".jsonl":
                continue
            with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        sequence += 1
                        entry = (str(row.get("time", row.get("timestamp", ""))), sequence, row)
                        heapq.heappush(rows, entry)
                        if len(rows) > last:
                            heapq.heappop(rows)
    # Stable sorting also handles sessions with interleaved modification times.
    return [entry[2] for entry in sorted(rows)]
