"""Explicit, local recording of command fixtures; this module never executes plans."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time
import uuid
import wave
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .catalog import LoadedCatalog
from .parser import parse
from .schema import validate_plan

SAMPLE_RATE = 16000
MAX_SECONDS = 20.0
STARTUP_TIMEOUT = 5.0
_CLEANUP_TIMEOUT = 2.0
_CAPTURE_CLEANUP_FAILED = False
_WAV_NAME = re.compile(r"p\d{3}-take-[0-9a-f]{32}\.wav")


class RecordingError(ValueError):
    """A recording/session problem suitable for a plain CLI message."""


class RecordingCleanupError(RecordingError):
    """A capture worker did not close; prohibit another take in this process."""


@dataclass(frozen=True)
class CaptureResult:
    pcm16: bytes
    device: dict
    status_flags: tuple[str, ...] = ()
    capped: bool = False
    readiness: dict | None = None


@dataclass(frozen=True)
class RecordingSession:
    path: Path
    manifest: dict
    completed: bool


class Recorder(Protocol):
    def device_info(self) -> dict: ...

    def capture(self, stop: threading.Event, max_seconds: float, *, ready: threading.Event,
                startup_timeout: float) -> CaptureResult: ...


class DefaultMicrophone:
    """Lazy sounddevice adapter. Construction and device queries do not capture."""

    def __init__(self, sounddevice_module=None):
        self._sd = sounddevice_module

    def _module(self):
        if self._sd is None:
            import sounddevice

            self._sd = sounddevice
        return self._sd

    def device_info(self) -> dict:
        sd = self._module()
        try:
            info = dict(sd.query_devices(kind="input"))
            host = dict(sd.query_hostapis(info["hostapi"]))
        except Exception as exc:
            raise RecordingError(f"Cannot query the default microphone: {exc}") from exc
        return {
            "index": int(info["index"]),
            "name": str(info["name"]),
            "hostapi": str(host["name"]),
            "default_samplerate": float(info["default_samplerate"]),
            "selection": "system-default-input",
        }

    def capture(self, stop: threading.Event, max_seconds: float, *, ready: threading.Event,
                startup_timeout: float = STARTUP_TIMEOUT) -> CaptureResult:
        opened_at = time.monotonic()
        startup_deadline = opened_at + startup_timeout
        sd = self._module()
        device = self.device_info()
        sd.check_input_settings(
            device=device["index"], channels=1, dtype="int16", samplerate=SAMPLE_RATE
        )
        chunks: list[bytes] = []
        flags: set[str] = set()
        byte_limit = int(max_seconds * SAMPLE_RATE) * 2
        received = 0
        capped = False
        first_frame_at = None

        def callback(indata, frames, timing, status):
            nonlocal received, capped, first_frame_at
            if stop.is_set():
                return
            if status:
                flags.add(str(status))
            data = bytes(indata)
            if len(data) != frames * 2:
                flags.add("unexpected frame size")
                stop.set()
                return
            if not frames:
                return
            captured_at = time.monotonic()
            if first_frame_at is None and captured_at > startup_deadline:
                stop.set()
                return
            remaining = byte_limit - received
            chunks.append(data[:remaining])
            received += min(len(data), remaining)
            if first_frame_at is None:
                first_frame_at = captured_at
                # Buffer the first valid frame before the main thread may tell
                # the speaker to begin. Stream construction/start is not proof
                # that Windows has begun delivering microphone samples.
                ready.set()
            if received >= byte_limit:
                capped = True
                stop.set()

        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                device=device["index"], callback=callback,
            ):
                while not ready.is_set() and not stop.is_set():
                    remaining = startup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise RecordingError("Microphone startup timed out before the first audio frame")
                    ready.wait(min(0.02, remaining))
                if not ready.is_set() or first_frame_at is None:
                    raise RecordingError("Microphone stopped before its first audio frame")
                remaining = max(0.0, first_frame_at + max_seconds - time.monotonic())
                if not stop.wait(remaining):
                    capped = True
        except Exception as exc:
            raise RecordingError(f"Could not record from {device['name']}: {exc}") from exc
        # Windows sounddevice int16 buffers and WAV PCM are little endian.
        return CaptureResult(b"".join(chunks), device, tuple(sorted(flags)), capped, {
            "method": "first_valid_input_frame", "startup_ms": (first_frame_at - opened_at) * 1000,
            "startup_timeout_seconds": startup_timeout, "cap_starts": "first_valid_input_frame",
        })


# Source utterances come from the locked contract, not a parallel command grammar.
# The corresponding template only rewrites targets to this installed catalog.
_SELECTION = (
    ("open chrome", "open {browser}"),
    ("open cloud", "open {claude}"),
    ("launch fusion", "launch {fusion}"),
    ("open word document", "open {word} document"),
    ("open the calculator app", "open the {calculator} app"),
    ("open vs code", "open {editor}"),
    ("open note pad", "open {notepad}"),
    ("switch to chrome", "switch to {browser}"),
    ("go to claude", "go to {claude}"),
    ("close claude", "close {claude}"),
    ("close claude, open chrome", "close {claude}, open {browser}"),
    ("open chrome and fusion", "open {browser} and {fusion}"),
    ("open word and excel", "open {word} and {excel}"),
    ("Open Fusion, make it full screen, and move it to the other screen.",
     "Open {fusion}, make it full screen, and move it to the other screen."),
    ("open notepad and type meeting at 3 pm, bring the laptop",
     "open {notepad} and type meeting at 3 pm, bring the laptop"),
    ("minimize chrome", "minimize {browser}"),
    ("restore the window", "restore the window"),
    ("move fusion to the second display", "move {fusion} to the second display"),
    ("snap chrome left", "snap {browser} left"),
    ("close this window", "close this window"),
    ("open youtube", "open {site_one}"),
    ("go to github", "go to {site_two}"),
    ("new tab", "new tab"),
    ("close this tab", "close this tab"),
    ("next tab", "next tab"),
    ("reopen closed tab", "reopen closed tab"),
    ("go back", "go back"),
    ("reload the page", "reload the page"),
    ("search for lofi hip hop", "search for lofi hip hop"),
    ("search youtube for flood fill algorithm", "search {engine} for flood fill algorithm"),
    ("type hello world and press enter", "type hello world and press enter"),
    ("write dear professor", "write dear professor"),
    ("press control shift t", "press control shift t"),
    ("hit escape", "hit escape"),
    ("scroll down a lot", "scroll down a lot"),
    ("scroll to the top", "scroll to the top"),
    ("volume up", "volume up"),
    ("pause the music", "pause the music"),
    ("start dictation", "start dictation"),
    ("stop listening", "stop listening"),
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _contract_utterances(root: Path) -> tuple[list[str], str]:
    path = root / "tests" / "test_parser_contract.py"
    try:
        raw = path.read_bytes()
        module = ast.parse(raw, filename=str(path))
        assignment = next(
            node for node in module.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "COMMANDS" for target in node.targets)
        )
        utterances = [ast.literal_eval(item.elts[0]) for item in assignment.value.elts]
        if len(utterances) < 117 or not all(isinstance(text, str) for text in utterances):
            raise ValueError("expected the 117 command utterances")
    except (OSError, SyntaxError, ValueError, StopIteration, AttributeError, TypeError) as exc:
        raise RecordingError(f"Cannot read locked parser command provenance: {exc}") from exc
    return utterances[:117], _digest(raw)


def build_prompts(root: Path, loaded: LoadedCatalog) -> tuple[list[dict], dict]:
    """Build exactly 40 commands and code-validated plans from the locked source."""
    utterances, contract_hash = _contract_utterances(root)
    counts = Counter(app.name for app in loaded.installed_apps)
    reachable = [name for name in sorted(counts) if counts[name] == 1
                 and parse(f"open {name}", loaded.catalog) == [{"action": "open_app", "app": name}]]
    if not reachable:
        raise RecordingError("No unique installed app can be used for recording prompts")

    def choose(*names):
        return next((name for name in names if name in reachable), reachable[0])

    sites = loaded.catalog.sites
    replacements = {
        "browser": choose(loaded.catalog.aliases.get("browser"), "Google Chrome", "Microsoft Edge"),
        "claude": choose("Claude", "Notepad"),
        "fusion": choose("Autodesk Fusion", "Calculator"),
        "word": choose("Word", "Notepad"),
        "excel": choose("Excel", "Calculator"),
        "calculator": choose("Calculator", "Notepad"),
        "notepad": choose("Notepad", "Calculator"),
        "editor": choose("Visual Studio Code", "Cursor", "Notepad"),
        "site_one": "youtube" if "youtube" in sites else "youtube dot com",
        "site_two": "github" if "github" in sites else "github dot com",
        "engine": "youtube" if "youtube" in loaded.catalog.engines else "google",
    }
    prompts = []
    for number, (source, template) in enumerate(_SELECTION, 1):
        if source not in utterances:
            raise RecordingError(f"Recording prompt source is absent from the first 117 commands: {source}")
        text = template.format_map(replacements)
        plan = parse(text, loaded.catalog)
        if plan is None:
            raise RecordingError(f"Installed-app recording prompt does not parse: {text}")
        validate_plan(plan)
        prompts.append({
            "id": f"p{number:03}", "expected_text": text, "expected_plan": plan,
            "source_index": utterances.index(source), "source_text": source,
        })
    provenance = {
        "contract_path": "tests/test_parser_contract.py", "contract_sha256": contract_hash,
        "prompt_set_sha256": _digest(_json_bytes(prompts)), "substitutions": replacements,
        "catalog_sha256": _digest(_json_bytes({
            "apps": [{"name": app.name, "app_id": app.app_id} for app in loaded.installed_apps],
            "aliases": loaded.catalog.aliases, "sites": sites, "engines": loaded.catalog.engines,
        })),
    }
    return prompts, provenance


def audio_quality(result: CaptureResult, max_seconds: float = MAX_SECONDS) -> dict:
    """Transparent signal checks; these do not judge recognition or spoken content."""
    pcm = result.pcm16
    reasons = []
    if len(pcm) % 2:
        reasons.append("incomplete PCM sample")
    samples = [item[0] for item in struct.iter_unpack("<h", pcm[:len(pcm) // 2 * 2])]
    count = len(samples)
    rms = math.sqrt(sum(value * value for value in samples) / count) / 32768 if count else 0.0
    peak = max((abs(value) for value in samples), default=0) / 32768
    clipped = sum(abs(value) >= 32760 for value in samples)
    fraction = clipped / count if count else 0.0
    if count == 0:
        reasons.append("empty recording")
    elif count < SAMPLE_RATE // 4:
        reasons.append("shorter than 0.25 seconds")
    if count and rms < 0.001:
        reasons.append("silent or extremely quiet recording")
    if fraction >= 0.01:
        reasons.append("at least 1 percent of samples clipped")
    if count > int(max_seconds * SAMPLE_RATE):
        reasons.append("recording exceeded its frame limit")
    if result.status_flags:
        reasons.append("capture reported lost or invalid audio")
    return {
        "accepted": not reasons, "reasons": reasons, "frames": count,
        "duration_seconds": count / SAMPLE_RATE, "rms": rms, "peak": peak,
        "clipped_fraction": fraction, "capture_status": list(result.status_flags),
        "capped": result.capped,
    }


def _atomic_json(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_wav(path: Path, pcm: bytes) -> str:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            with wave.open(handle, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            handle.flush()
            os.fsync(handle.fileno())
        data_hash = _digest(temporary.read_bytes())
        os.replace(temporary, path)
        return data_hash
    finally:
        temporary.unlink(missing_ok=True)


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _load_session(path: Path, prompts: list[dict], provenance: dict) -> dict:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest["version"] != 1 or manifest["provenance"] != provenance:
            raise ValueError("contract, catalog, or prompt provenance changed")
        if manifest["audio_format"] != {"sample_rate": SAMPLE_RATE, "channels": 1, "sample_width": 2}:
            raise ValueError("audio format changed")
        if len(manifest["prompts"]) != len(prompts):
            raise ValueError("prompt count changed")
        for expected, entry in zip(prompts, manifest["prompts"], strict=True):
            if any(entry.get(key) != value for key, value in expected.items()):
                raise ValueError(f"prompt {expected['id']} changed")
            if entry["status"] not in {"pending", "recorded", "skipped"}:
                raise ValueError("invalid prompt status")
            selected = None
            for attempt in entry["attempts"]:
                filename = attempt.get("wav")
                if filename is None:
                    continue
                if not _WAV_NAME.fullmatch(filename) or not filename.startswith(entry["id"] + "-"):
                    raise ValueError("invalid WAV path")
                wav_path = path / filename
                if wav_path.resolve().parent != path.resolve():
                    raise ValueError("WAV path leaves the recording session")
                if _digest(wav_path.read_bytes()) != attempt["wav_sha256"]:
                    raise ValueError(f"WAV checksum changed: {filename}")
                with wave.open(str(wav_path), "rb") as wav:
                    if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                        raise ValueError(f"WAV format changed: {filename}")
                    if wav.getnframes() != attempt["quality"]["frames"]:
                        raise ValueError(f"WAV length changed: {filename}")
                if attempt.get("decision") == "keep":
                    selected = attempt
            if entry["status"] == "recorded" and (
                selected is None or not selected["quality"]["accepted"]
                or entry.get("selected_wav") != selected["wav"]
            ):
                raise ValueError("recorded prompt has no accepted selected take")
        return manifest
    except (OSError, ValueError, KeyError, TypeError, wave.Error) as exc:
        raise RecordingError(f"Cannot resume recording session: {exc}") from exc


def _console_stop(done: threading.Event) -> None:
    """Read Enter without leaving a blocked reader behind when the capture caps."""
    if os.name != "nt" or not sys.stdin.isatty():
        raise RecordingError("Recording needs an interactive Windows terminal")
    import msvcrt

    while not done.wait(0.02):
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key == "\x03":
                raise KeyboardInterrupt
            if key in {"\r", "\n"}:
                return


def _capture(recorder: Recorder, max_seconds: float, stop_fn: Callable, *, on_ready: Callable,
             startup_timeout: float = STARTUP_TIMEOUT) -> CaptureResult:
    global _CAPTURE_CLEANUP_FAILED
    if _CAPTURE_CLEANUP_FAILED:
        raise RecordingCleanupError("A previous microphone worker did not close; exit this process before retrying.")
    stop, done, ready = threading.Event(), threading.Event(), threading.Event()
    result, errors = [], []

    def worker():
        try:
            result.append(recorder.capture(stop, max_seconds, ready=ready, startup_timeout=startup_timeout))
        except Exception as exc:  # noqa: BLE001 - transfer worker failures to the calling thread
            errors.append(exc)
        finally:
            done.set()

    # If a native driver never returns, a daemon permits the CLI process to
    # exit and Windows to close its handles. Never start another take after a
    # cleanup failure. Python cannot forcibly cancel a blocked native call.
    thread = threading.Thread(target=worker, name="fixture-capture", daemon=True)
    deadline = time.monotonic() + startup_timeout
    thread.start()
    try:
        while not ready.is_set():
            if done.is_set():
                if errors:
                    raise RecordingError(str(errors[0])) from errors[0]
                raise RecordingError("Capture returned without confirming its first audio frame")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecordingError("Microphone startup timed out before the first audio frame")
            ready.wait(min(0.02, remaining))
        if errors:
            raise RecordingError(str(errors[0])) from errors[0]
        if done.is_set():
            raise RecordingError("Capture ended before the Ready cue; retry this take")
        on_ready()
        # Console input is consumed only once audio delivery is confirmed;
        # failed startup cannot leave a stop reader waiting on Enter.
        stop_fn(done)
    finally:
        stop.set()
        thread.join(_CLEANUP_TIMEOUT)
        if thread.is_alive():
            _CAPTURE_CLEANUP_FAILED = True
            raise RecordingCleanupError("Microphone worker did not close. Stop recording and exit this process before retrying.")
    if errors:
        raise RecordingError(str(errors[0])) from errors[0]
    if not result or not isinstance(result[0], CaptureResult):
        raise RecordingError("Capture returned no audio result")
    return result[0]


def _console_output(text: str) -> None:
    print(text, flush=True)


def record_set(
    root: Path,
    loaded: LoadedCatalog,
    *,
    session_label: str = "quiet",
    session_dir: Path | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = _console_output,
    recorder: Recorder | None = None,
    stop_fn: Callable[[threading.Event], None] | None = None,
    max_seconds: float = MAX_SECONDS,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> RecordingSession:
    """Record or resume 40 prompts, requiring explicit start and take acceptance.

    ``stop_fn(done)`` waits for Enter or capture completion; tests inject it along
    with a fake recorder. No recognizer, executor, or network client is imported.
    """
    if _CAPTURE_CLEANUP_FAILED:
        raise RecordingCleanupError("A previous microphone worker did not close; exit this process before retrying.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", session_label):
        raise RecordingError("Session label must be 1-32 letters, digits, underscores, or hyphens")
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_SECONDS:
        raise RecordingError("Recording cap must be greater than zero and at most 20 seconds")
    if not math.isfinite(startup_timeout) or not 0 < startup_timeout <= 30:
        raise RecordingError("Microphone startup timeout must be greater than zero and at most 30 seconds")
    if recorder is None and stop_fn is None and (os.name != "nt" or not sys.stdin.isatty()):
        raise RecordingError("Recording needs an interactive Windows terminal")
    root = Path(root).resolve()
    prompts, provenance = build_prompts(root, loaded)
    base = (root / "fixtures" / "mine").resolve()
    base.mkdir(parents=True, exist_ok=True)
    if session_dir is None:
        path = base / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{session_label}-{uuid.uuid4().hex[:8]}"
        path.mkdir()
        manifest = {
            "version": 1, "created_at": _utc(), "session_label": session_label,
            "audio_format": {"sample_rate": SAMPLE_RATE, "channels": 1, "sample_width": 2},
            "max_seconds": max_seconds, "provenance": provenance,
            "capture_protocol": {"readiness": "first_valid_input_frame", "startup_timeout_seconds": startup_timeout},
            "prompts": [{**prompt, "status": "pending", "attempts": []} for prompt in prompts],
        }
        _atomic_json(path / "manifest.json", manifest)
    else:
        path = Path(session_dir).resolve()
        if path.parent != base:
            raise RecordingError("Resume directory must be a direct session under fixtures/mine")
        manifest = _load_session(path, prompts, provenance)
        if manifest["max_seconds"] != max_seconds:
            raise RecordingError("Resume recording cap must match its saved session")
    recorder = recorder or DefaultMicrophone()
    stop_fn = stop_fn or _console_stop
    output_fn(f"Session: {path}\nRecording only. No commands will execute.")
    device = recorder.device_info()
    output_fn(f"Default microphone: {device.get('name', 'unknown')}")

    def save():
        manifest["updated_at"] = _utc()
        _atomic_json(path / "manifest.json", manifest)

    try:
        for number, entry in enumerate(manifest["prompts"], 1):
            if entry["status"] != "pending":
                continue
            while entry["status"] == "pending":
                output_fn(f"\n{number}/40: {entry['expected_text']}")
                command = input_fn("Enter starts recording; s skips; q quits: ").strip().lower()
                if command == "q":
                    save()
                    return RecordingSession(path, manifest, False)
                if command == "s":
                    entry["status"] = "skipped"
                    save()
                    break
                if command:
                    output_fn("Use Enter, s, or q.")
                    continue
                output_fn("Starting microphone; wait for the Ready cue before speaking.")
                try:
                    captured = _capture(
                        recorder, max_seconds, stop_fn, startup_timeout=startup_timeout,
                        on_ready=lambda: output_fn(f"Ready — say the command. Press Enter to stop; automatic cap {max_seconds:g} seconds."),
                    )
                except RecordingCleanupError as exc:
                    entry["attempts"].append({"captured_at": _utc(), "error": str(exc), "decision": "quit"})
                    save()
                    raise
                except RecordingError as exc:
                    entry["attempts"].append({"captured_at": _utc(), "error": str(exc), "decision": "retry"})
                    save()
                    output_fn(str(exc))
                    continue
                quality = audio_quality(captured, max_seconds)
                attempt = {
                    "captured_at": _utc(), "device": captured.device, "quality": quality,
                    "readiness": captured.readiness,
                    "decision": None, "wav": None,
                }
                if captured.pcm16 and len(captured.pcm16) % 2 == 0:
                    filename = f"{entry['id']}-take-{uuid.uuid4().hex}.wav"
                    attempt["wav_sha256"] = _atomic_wav(path / filename, captured.pcm16)
                    attempt["wav"] = filename
                entry["attempts"].append(attempt)
                save()
                output_fn(f"Captured {quality['duration_seconds']:.2f}s; RMS {quality['rms']:.4f}; "
                          f"clipped {quality['clipped_fraction']:.2%}.")
                if quality["reasons"]:
                    output_fn("Retake needed: " + "; ".join(quality["reasons"]))
                while True:
                    prompt = "Enter keeps; r retries; s skips; q quits: " if quality["accepted"] else "Enter/r retries; s skips; q quits: "
                    choice = input_fn(prompt).strip().lower()
                    if choice == "" and quality["accepted"]:
                        attempt["decision"] = "keep"
                        entry["status"], entry["selected_wav"] = "recorded", attempt["wav"]
                    elif choice in {"", "r"}:
                        attempt["decision"] = "retry"
                    elif choice == "s":
                        attempt["decision"], entry["status"] = "skip", "skipped"
                    elif choice == "q":
                        attempt["decision"] = "quit"
                        save()
                        return RecordingSession(path, manifest, False)
                    else:
                        output_fn("Use Enter, r, s, or q.")
                        continue
                    save()
                    break
    except (KeyboardInterrupt, EOFError):
        save()
        output_fn("Recording stopped; session saved for resume.")
        return RecordingSession(path, manifest, False)
    recorded = sum(entry["status"] == "recorded" for entry in manifest["prompts"])
    completed = recorded == 40
    manifest["completed"] = completed
    save()
    output_fn(f"Saved {recorded}/40 recordings. Stop here; ask for approval before step 6 benchmarks.")
    return RecordingSession(path, manifest, completed)
