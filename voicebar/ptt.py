"""Windows push-to-talk: bounded capture, streaming recognition, then validated plans."""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .parser import contains_app_phrase_prefix, parse
from .schema import validate_plan
from .stt.base import SAMPLE_RATE, TextEvent

FRAME_SAMPLES = 512
_CAPTURE_UNSAFE = threading.Event()


class PttError(RuntimeError):
    """Push-to-talk stopped without executing an incomplete or invalid utterance."""


class PttCleanupError(PttError):
    """Native capture closure is unconfirmed; require process exit before retrying."""


@dataclass(frozen=True)
class Hotkey:
    text: str
    modifiers: int
    key: int
    held_keys: tuple[int, ...]


@dataclass(frozen=True)
class PttReport:
    utterances: int
    last_result: dict | None
    log_path: Path


@dataclass(frozen=True)
class PttExecution:
    results: list[dict]
    action_started_at: float | None = None


def parse_hotkey(text: str) -> Hotkey:
    if not isinstance(text, str):
        raise PttError("PTT hotkey must be text, for example ctrl+alt+f8")
    parts = [part.strip().lower().replace("control", "ctrl") for part in text.split("+")]
    if len(parts) < 2 or len(parts) != len(set(parts)):
        raise PttError("PTT hotkey needs distinct modifiers and one trigger key")
    modifiers = {"ctrl": (2, 0x11), "alt": (1, 0x12), "shift": (4, 0x10)}
    if any(part not in modifiers for part in parts[:-1]):
        raise PttError("PTT modifiers may be ctrl, alt, and shift; Windows-key shortcuts are reserved")
    trigger = parts[-1]
    if re.fullmatch(r"f(?:[1-9]|1[01])", trigger):
        key = 0x70 + int(trigger[1:]) - 1
    elif len(trigger) == 1 and trigger.isascii() and trigger.isalnum():
        key = ord(trigger.upper())
    elif trigger == "space":
        key = 0x20
    else:
        raise PttError("PTT trigger must be a letter, digit, space, or F1-F11; F12 is reserved")
    if set(parts) == {"alt", "space"}:
        raise PttError("Alt+Space is reserved for the window menu and PowerToys Run")
    names = [name for name in modifiers if name in parts[:-1]]
    return Hotkey("+".join([*names, trigger]), sum(modifiers[name][0] for name in names),
                  key, (key, *(modifiers[name][1] for name in names)))


class WindowsHotkey:
    """Thread-local RegisterHotKey message pump; no keyboard hooks or windows."""

    def __init__(self, hotkey: Hotkey):
        if os.name != "nt":
            raise PttError("Push-to-talk requires native Windows")
        from ctypes import wintypes as w

        self.hotkey, self.registered, self.identifier = hotkey, False, 0x5642
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.message_type = w.MSG
        declarations = {
            "RegisterHotKey": ([w.HWND, ctypes.c_int, w.UINT, w.UINT], w.BOOL),
            "UnregisterHotKey": ([w.HWND, ctypes.c_int], w.BOOL),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "PeekMessageW": ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL),
            "OpenInputDesktop": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "GetUserObjectInformationW": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            "CloseDesktop": ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result

    def register(self) -> None:
        if self.registered:
            raise PttError("PTT hotkey is already registered on this controller")
        if not self.user32.RegisterHotKey(None, self.identifier, self.hotkey.modifiers | 0x4000,
                                         self.hotkey.key):
            raise PttError(f"Cannot register {self.hotkey.text}: hotkey collision or Windows error "
                           f"{ctypes.get_last_error()}; choose another PTT shortcut")
        self.registered = True

    def held(self) -> bool:
        return all(self.user32.GetAsyncKeyState(key) & 0x8000 for key in self.hotkey.held_keys)

    def available(self) -> bool:
        # The secure/locked desktop must never keep capture or execution active.
        desktop = self.user32.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not desktop:
            return False
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_ulong()
            return bool(self.user32.GetUserObjectInformationW(
                desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
            )) and name.value.casefold() == "default"  # UOI_NAME
        finally:
            self.user32.CloseDesktop(desktop)

    def wait_pressed(self, stop: threading.Event) -> bool:
        message = self.message_type()
        while not stop.is_set():
            # Only this thread's messages: this CLI controller owns no GUI window.
            while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x12:  # WM_QUIT
                    stop.set()
                    return False
                if (message.message == 0x312 and message.wParam == self.identifier
                        and self.available() and self.held()):
                    return True
            stop.wait(0.01)
        return False

    def close(self) -> None:
        if self.registered:
            self.user32.UnregisterHotKey(None, self.identifier)
            self.registered = False


def probe_hotkey(text: str = "ctrl+alt+f8") -> dict:
    """Explicit registration-only probe; never opens audio or loads a model."""
    hotkey = parse_hotkey(text)
    controller = WindowsHotkey(hotkey)
    try:
        controller.register()
        return {"hotkey": hotkey.text, "available": True, "microphone_opened": False}
    finally:
        controller.close()


class DefaultInput:
    """Callback copies PCM to a bounded queue; inference never runs in PortAudio."""

    is_open = False

    @contextmanager
    def open(self, callback):
        import sounddevice as sd

        info = dict(sd.query_devices(kind="input"))
        device = {"name": info["name"], "index": int(info["index"]),
                  "selection": "system-default-input", "sample_rate": SAMPLE_RATE,
                  "hostapi": sd.query_hostapis(info["hostapi"])["name"]}
        sd.check_input_settings(device=device["index"], channels=1, dtype="int16", samplerate=SAMPLE_RATE)

        def receive(indata, frames, timing, status):
            callback(bytes(indata), frames, str(status) if status else "")

        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                   device=device["index"], blocksize=FRAME_SAMPLES, callback=receive)
        self.is_open = True
        try:
            stream.start()
            yield device
        finally:
            stream.close(ignore_errors=False)  # confirm native closure; discard pending input
            self.is_open = False


def _default_execute(root, loaded, plan, *, monitor_reference="window") -> PttExecution:
    from .executor import Context, execute

    with Context(root, loaded=loaded, monitor_reference=monitor_reference) as context:
        entered = time.perf_counter()
        results = execute(plan, context=context)
    started = next((item.get("started_ms") for item in results if item.get("started_ms") is not None), None)
    return PttExecution(results, entered + started / 1000 if started is not None else None)


def _default_state(root):
    from .executor.common import read_state

    return read_state(root).get("last_app")


def _say(output_fn, text):
    if output_fn is print:
        print(text, flush=True)
    else:
        output_fn(text)


def _hold(backend, controls, microphone, stop, shutdown, close_backend, workers,
          output_fn, diagnostics, *, max_seconds, backlog_seconds, recognition_timeout,
          startup_timeout, cleanup_timeout) -> dict:
    import numpy as np

    audio = queue.Queue(maxsize=max(1, int(backlog_seconds * SAMPLE_RATE / FRAME_SAMPLES)))
    display = queue.Queue(maxsize=256)
    ready, done, capture_done, active, cancel, first_frame = (threading.Event() for _ in range(6))
    capture_stop, capture_finished = threading.Event(), threading.Event()
    errors, events = [], []
    lock = threading.Lock()
    captured_frames = 0
    energy = 0.0
    processed_frames = 0
    began = time.perf_counter()
    released, closed_at, device_info = None, None, None
    capture_closed, ready_announced = True, False
    first_frame_at, capture_began, capture_worker, cleanup_error, cleanup_interrupt = None, None, None, None, None

    def fail(error):
        with lock:
            if not errors:
                errors.append(error)
            cancel.set()
            active.clear()

    def deactivate():
        nonlocal released
        # Serialize the cutoff with callback enqueue so no accepted callback
        # can append PCM after the controller records this release/cancellation.
        with lock:
            if capture_worker is not None and released is None:
                released = time.perf_counter()
            active.clear()

    def emit(event):
        if not isinstance(event, TextEvent) or not isinstance(event.text, str) or type(event.final) is not bool:
            raise PttError("Recognizer returned a malformed text event")
        if len(events) >= 1000:
            raise PttError("Recognizer exceeded the event limit")
        events.append(event)
        try:
            display.put_nowait(event)
        except queue.Full as error:
            raise PttError("Recognition feedback backlog exceeded its limit") from error

    def recognize():
        nonlocal energy, processed_frames
        try:
            backend.start(emit)
            ready.set()
            while not cancel.is_set():
                try:
                    pcm = audio.get(timeout=0.01)
                except queue.Empty:
                    if capture_done.is_set():
                        break
                    continue
                chunk = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                energy += float(np.dot(chunk, chunk))
                processed_frames += len(chunk)
                backend.feed(chunk)
            if not cancel.is_set():
                backend.finish()
        except BaseException as error:  # noqa: BLE001 - transfer worker failures to the controller
            fail(error)
        finally:
            ready.set()
            done.set()
            if shutdown.is_set():
                close_backend()

    def receive(pcm, frames, status):
        nonlocal captured_frames, first_frame_at
        with lock:
            if not active.is_set() or cancel.is_set():
                return
            problem = None
            if status:
                problem = PttError(f"Microphone reported lost or invalid audio: {status}")
            elif not isinstance(pcm, bytes) or not 0 < frames <= FRAME_SAMPLES or len(pcm) != frames * 2:
                problem = PttError("Microphone returned malformed PCM frames")
            elif first_frame_at is None and time.perf_counter() - capture_began >= startup_timeout:
                problem = PttError("Microphone startup timed out before its first audio frame")
            elif captured_frames + frames > int(max_seconds * SAMPLE_RATE):
                problem = PttError("PTT reached its recording cap; the incomplete command was discarded")
            if problem is not None:
                if not errors:
                    errors.append(problem)
                cancel.set()
                active.clear()
                return
            try:
                audio.put_nowait(pcm)
                captured_frames += frames
                if first_frame_at is None:
                    first_frame_at = time.perf_counter()
                first_frame.set()
            except queue.Full:
                if not errors:
                    errors.append(PttError("Recognition audio backlog exceeded its limit; no command ran"))
                cancel.set()
                active.clear()

    def capture():
        nonlocal capture_closed, closed_at, device_info
        try:
            # Open, start and close may block inside a native driver. Keep them
            # off the controller so key release, lock and deadlines still work.
            with microphone.open(receive) as device_info:
                capture_stop.wait()
            capture_closed = True
        except BaseException as error:  # noqa: BLE001 - transfer audio worker failures to the controller
            fail(error)
        finally:
            if hasattr(microphone, "is_open"):
                capture_closed = not microphone.is_open
            if capture_closed:
                closed_at = time.perf_counter()
            capture_finished.set()

    def feedback():
        nonlocal ready_announced
        if first_frame.is_set() and active.is_set() and not ready_announced:
            if controls.held():
                _say(output_fn, "Ready — speak")
                ready_announced = True
            else:
                deactivate()
                capture_stop.set()
        for _ in range(16):
            try:
                event = display.get_nowait()
            except queue.Empty:
                break
            if not event.final:
                output_fn(json.dumps({"kind": "partial", "text": event.text,
                                      "line_id": event.line_id}, ensure_ascii=False))

    worker = threading.Thread(target=recognize, name="ptt-recognition", daemon=True)
    workers.append(worker)
    worker.start()
    try:
        while not ready.wait(0.005):
            if stop.is_set() or not controls.available() or not controls.held():
                cancel.set()
                break
            if time.perf_counter() - began > recognition_timeout:
                fail(PttError("Recognition did not start within its deadline"))
                break
        if not cancel.is_set() and controls.available() and controls.held() and not stop.is_set():
            active.set()
            capture_closed = None  # a failed adapter close cannot be presumed successful
            capture_began = time.perf_counter()
            capture_worker = threading.Thread(target=capture, name="ptt-capture", daemon=True)
            capture_worker.start()
            while not cancel.is_set():
                if stop.is_set():
                    cancel.set()
                    break
                if not controls.available():
                    fail(PttError("Windows locked or switched to a secure desktop; command discarded"))
                    break
                if not controls.held():
                    if not first_frame.is_set():
                        cancel.set()  # release during opening authorizes no later samples
                    break
                if capture_finished.is_set():
                    fail(PttError("Microphone stopped unexpectedly; command discarded"))
                    break
                now = time.perf_counter()
                if first_frame_at is None and now - capture_began >= startup_timeout:
                    fail(PttError("Microphone startup timed out before its first audio frame"))
                    break
                if first_frame_at is not None and now - first_frame_at >= max_seconds:
                    fail(PttError("PTT reached its recording cap; the incomplete command was discarded"))
                    break
                feedback()
                stop.wait(0.005)
        else:
            cancel.set()
    except BaseException:
        cancel.set()
        raise
    finally:
        deactivate()
        capture_stop.set()
        if capture_worker is not None:
            close_deadline = time.perf_counter() + cleanup_timeout
            try:
                while not capture_finished.is_set():
                    try:
                        capture_finished.wait(0.005)
                        if not controls.available():
                            fail(PttError("Windows locked during microphone cleanup; command discarded"))
                    except BaseException as error:  # noqa: BLE001 - complete cleanup after interruption
                        cleanup_interrupt = cleanup_interrupt or error
                        fail(error)
                    if stop.is_set():
                        cancel.set()
                    if time.perf_counter() >= close_deadline:
                        break
            except BaseException as error:  # noqa: BLE001 - preserve closure boundary on controller errors
                cleanup_interrupt = cleanup_interrupt or error
                fail(error)
            finally:
                if not capture_finished.is_set() or capture_closed is not True:
                    _CAPTURE_UNSAFE.set()
                    cleanup_error = PttCleanupError(
                        "Microphone cleanup could not be confirmed. Exit this process before retrying; no command ran."
                    )
                    fail(cleanup_error)
                    shutdown.set()
                # Preserve this even if recognition waiting is itself interrupted.
                diagnostics.update(capture_closed=capture_closed, captured_frames=captured_frames,
                                   process_exit_required=cleanup_error is not None)
        capture_done.set()
        deadline = time.perf_counter() + recognition_timeout
        while not done.wait(0.005):
            if not controls.available():
                fail(PttError("Windows locked during recognition; command discarded"))
            if time.perf_counter() >= deadline:
                shutdown.set()
                fail(PttError("Recognition did not finish within its deadline; no command ran"))
                break
            if stop.is_set():
                cancel.set()
        if done.is_set() and not shutdown.is_set():
            worker.join()
        diagnostics.update({
            "backend": backend.name, "device": backend.device, "audio_device": device_info,
            "captured_frames": captured_frames, "capture_closed": capture_closed,
            "ready_cue_emitted": ready_announced, "released_at": released,
            "process_exit_required": cleanup_error is not None,
            "errors": [str(error) or type(error).__name__ for error in errors],
            "release_to_microphone_closed_ms": (max(0.0, (closed_at - released) * 1000)
                                                 if released is not None and closed_at is not None else None),
            "events": [asdict(event) for event in events],
        })
        if cleanup_error is not None:
            raise cleanup_error
        if cleanup_interrupt is not None:
            raise cleanup_interrupt
    feedback()
    if errors:
        raise PttError(str(errors[0])) from errors[0]
    finals = {event.line_id: event for event in events if event.final}
    ordered = sorted(finals.values(), key=lambda event: (event.start_sample, event.line_id))
    text = " ".join(event.text.strip() for event in ordered if event.text.strip())
    rms = math.sqrt(energy / processed_frames) if processed_frames else 0.0
    if rms < 0.001:
        text = ""  # silence cannot turn a hallucinated phrase into an action
    last_final = max((event.emitted_at for event in ordered), default=None)
    return {
        **diagnostics,
        "backend": backend.name, "device": backend.device, "text": text,
        "status": "cancelled" if cancel.is_set() else "recognized",
        "rms": rms,
        "release_to_final_ms": (max(0.0, (last_final - released) * 1000)
                                if released is not None and last_final is not None else None),
        "release_to_microphone_closed_ms": (max(0.0, (closed_at - released) * 1000)
                                             if released is not None else None),
        "released_at": released, "last_speech_to_final_ms": None,
        "events": [asdict(event) for event in events], "executed": False,
    }


def listen_ptt(
    root: Path, loaded, *, backend_name: str = "moonshine", device: str = "cpu",
    hotkey: str = "ctrl+alt+f8", dry_run: bool = False, stop_event=None,
    max_utterances: int | None = None, hotkeys=None, microphone=None, backend_factory=None,
    executor=None, state_reader=None, resolver=None, foreground_app=None,
    output_fn: Callable[[str], None] = print,
    max_seconds: float = 20.0, backlog_seconds: float = 2.0, recognition_timeout: float = 30.0,
    startup_timeout: float = 5.0, cleanup_timeout: float = 5.0,
) -> PttReport:
    """Listen only while the configured chord is held. Ctrl+C closes everything.

    Injected execution receives a fully validated plan, allowing an owned-window
    context for the human gate. It may return PttExecution for mutation timings.
    Native microphone calls run in a daemon worker. If closure cannot be
    confirmed within cleanup_timeout, require process exit: Python cannot kill
    a blocked driver call, and this process must never start another capture.
    """
    for value, label, upper in ((max_seconds, "capture cap", 20),
                                (backlog_seconds, "audio backlog", 5),
                                (recognition_timeout, "recognition timeout", 120),
                                (startup_timeout, "microphone startup timeout", 30),
                                (cleanup_timeout, "microphone cleanup timeout", 30)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= upper:
            raise PttError(f"PTT {label} must be greater than zero and at most {upper} seconds")
    if max_utterances is not None and (type(max_utterances) is not int or max_utterances < 1):
        raise PttError("max_utterances must be a positive integer")
    if _CAPTURE_UNSAFE.is_set():
        raise PttCleanupError("A previous microphone operation did not close safely. Exit this process before retrying.")
    root, parsed = Path(root).resolve(), parse_hotkey(hotkey)
    stop, shutdown = stop_event or threading.Event(), threading.Event()
    controls = hotkeys or WindowsHotkey(parsed)
    microphone = microphone or DefaultInput()
    if backend_factory is None:
        from .stt import load_backend

        backend_factory = load_backend
    monitor_reference = "window"
    executor = executor or (lambda plan: _default_execute(
        root, loaded, plan, monitor_reference=monitor_reference))
    state_reader = state_reader or (lambda: _default_state(root))
    backend, workers, closed = None, [], False
    close_lock = threading.Lock()
    from .logs import DecisionLog
    decision_log = DecisionLog(root, prefix="ptt")
    log_path = decision_log.path
    count, last, current = 0, None, None

    def write_result(result):
        result["timestamp"] = datetime.now(UTC).isoformat()
        serialized = json.dumps(result, ensure_ascii=False)
        decision_log(result)
        _say(output_fn, serialized)

    def close_backend():
        nonlocal closed
        with close_lock:
            if backend is not None and not closed:
                closed = True
                backend.close()

    try:
        controls.register()  # a collision fails before model loading or microphone capture
        from .catalog import _yaml_file
        from .resolver import configured_resolver, resolve
        from .resolver.candidates import strip_wake_name

        settings_path = root / "config/settings.yaml"
        settings = _yaml_file(settings_path) if settings_path.exists() else {}
        names = settings.get("names", [settings.get("name", "computer")])
        _say(output_fn, f"Loading {backend_name}; microphone closed.")
        backend = backend_factory(backend_name, root, loaded.catalog, device=device)
        if resolver is None:
            resolver = configured_resolver(root) if settings_path.exists() else resolve
        _say(output_fn, f"Hold {parsed.text}, wait for ‘Ready — speak’, then talk; release to finish. "
                       "Ctrl+C stops. Microphone closed while waiting.")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        while not stop.is_set() and controls.wait_pressed(stop):
            if not controls.available() or not controls.held():
                continue
            current = {"backend": backend.name, "device": backend.device,
                       "hotkey": parsed.text, "dry_run": dry_run, "executed": False,
                       "execution_attempted": False, "capture_closed": True,
                       "text": "", "plan": None, "last_speech_to_final_ms": None}
            result = _hold(backend, controls, microphone, stop, shutdown, close_backend, workers,
                           output_fn, current, max_seconds=max_seconds, backlog_seconds=backlog_seconds,
                           recognition_timeout=recognition_timeout, startup_timeout=startup_timeout,
                           cleanup_timeout=cleanup_timeout)
            current = result
            result["hotkey"], result["dry_run"] = parsed.text, dry_run
            last_app = state_reader()
            if last_app not in loaded.catalog.apps or loaded.catalog.apps.count(last_app) != 1:
                last_app = None
            body = strip_wake_name(result["text"], {"names": names})
            active_app = None
            if loaded.catalog.app_actions and foreground_app is not None:
                active_app = foreground_app()
            began = time.perf_counter()
            plan = parse(result["text"] if body is None else body,
                         loaded.catalog, last_app=last_app, active_app=active_app)
            result["timings"] = {"parser_ms": (time.perf_counter() - began) * 1000}
            result["tier"] = "tier1"
            # Holding a hotkey still authorizes ordinary nameless tier-1 PTT.
            # Only a literal spoken wake name can disclose speech to tier 2.
            if (plan is None and body and result["status"] != "cancelled"
                    and not stop.is_set() and controls.available()
                    and not contains_app_phrase_prefix(result["text"], loaded.catalog)):
                began = time.perf_counter()
                try:
                    resolver_context = {"names": names, "last_app": last_app}
                    if active_app is not None:
                        resolver_context["active_app"] = active_app
                    plan = resolver(result["text"], loaded.catalog, resolver_context)
                    if plan is not None:
                        validate_plan(plan)
                except Exception:  # noqa: BLE001 - optional selection fails closed.
                    plan = None
                result["timings"]["resolver_ms"] = (time.perf_counter() - began) * 1000
                result["tier"] = "tier2"
            if plan is not None:
                validate_plan(plan)  # full preflight before any executor/context import
                from .executor.windows import monitor_reference_for

                try:
                    monitor_reference = monitor_reference_for(result["text"], plan)
                except RuntimeError as error:
                    plan = None
                    result["detail"] = str(error)
            result.update(event="decision", reason="run" if plan is not None else "unresolved",
                          plan=plan)
            released = result["released_at"]
            result["release_to_plan_ms"] = ((time.perf_counter() - released) * 1000
                                             if released is not None else None)
            result["release_to_action_started_ms"] = None
            if plan is not None and result["status"] != "cancelled" and not dry_run and not stop.is_set():
                if not controls.available():
                    raise PttError("Windows locked before execution; no command ran")
                result["execution_attempted"] = True
                outcome = executor(plan)
                if isinstance(outcome, PttExecution):
                    result["results"] = outcome.results
                    if released is not None and outcome.action_started_at is not None:
                        result["release_to_action_started_ms"] = max(0.0, (outcome.action_started_at - released) * 1000)
                else:
                    result["results"] = outcome
                result["executed"] = any(item.get("ok") or item.get("started_ms") is not None
                                         for item in result["results"])
            write_result(result)
            count, last = count + 1, result
            current = None
            workers[:] = [worker for worker in workers if worker.is_alive()]
            if max_utterances is not None and count >= max_utterances:
                break
        return PttReport(count, last, log_path)
    except BaseException as error:  # log failures and cancellation after capture cleanup
        failure = dict(current or {
            "backend": backend_name, "device": device, "hotkey": parsed.text,
            "dry_run": dry_run, "text": "", "plan": None, "capture_closed": True,
            "executed": False, "execution_attempted": False,
        })
        failure.update(status="error", error=str(error) or type(error).__name__,
                       error_type=type(error).__name__)
        if failure["execution_attempted"]:
            failure["executed"] = None  # an executor exception cannot prove whether a mutation began
        write_result(failure)
        raise
    finally:
        shutdown.set()
        try:
            if not any(worker.is_alive() for worker in workers):
                close_backend()
        finally:
            controls.close()
