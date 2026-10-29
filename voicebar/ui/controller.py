"""UI-independent local runtime; every public control only enqueues a message.

Capture, segmentation, recognition, execution and model initialization never
run on the GUI thread. Capture epochs and GateSession generations revoke work
on Off, lock, suspend, microphone changes and shutdown. A released hold closes
its stream before its one complete recording becomes eligible for recognition.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ..catalog import _yaml_file
from ..diagnostics import explain_failure
from ..listening import DecisionLog, GateSession, configured_gate
from ..ptt import DefaultInput


class SelectedInput(DefaultInput):
    """The existing input format, with an explicit PortAudio device index."""

    def __init__(self, index=None):
        self.index = index
        self.is_open = False

    @contextmanager
    def open(self, callback):
        if self.index is None:
            with super().open(callback) as device:
                yield device
            return
        import sounddevice as sd

        from ..ptt import FRAME_SAMPLES
        from ..stt.base import SAMPLE_RATE

        info = dict(sd.query_devices(self.index, kind="input"))
        device = {"name": info["name"], "index": int(info["index"]),
                  "selection": "configured-input", "sample_rate": SAMPLE_RATE,
                  "hostapi": sd.query_hostapis(info["hostapi"])["name"]}
        sd.check_input_settings(device=self.index, channels=1, dtype="int16", samplerate=SAMPLE_RATE)

        def receive(indata, frames, timing, status):
            callback(bytes(indata), frames, str(status) if status else "")

        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                   device=self.index, blocksize=FRAME_SAMPLES, callback=receive)
        self.is_open = True
        try:
            stream.start()
            yield device
        finally:
            stream.close(ignore_errors=False)
            self.is_open = False


def input_devices():
    """Enumeration is called on a runtime worker, never a tray menu callback."""
    import sounddevice as sd

    return [{"index": int(device["index"]), "name": device["name"]}
            for device in sd.query_devices() if device["max_input_channels"] > 0]


class RuntimeController:
    def __init__(self, root, loaded, *, backend_name=None, device=None,
                 execution_timeout=35.0, recognition_timeout=30.0,
                 startup_timeout=5.0, frame_timeout=2.0, cleanup_timeout=3.0,
                 backend_factory=None, microphone_factory=None,
                 segmenter_factory=None, executor=None, available=None,
                 devices=None, foreground_app=None):
        for value in (execution_timeout, recognition_timeout, startup_timeout,
                      frame_timeout, cleanup_timeout):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError("Runtime deadlines must be finite positive seconds")
        self.root, self.loaded = Path(root), loaded
        settings = _yaml_file(self.root / "config/settings.yaml")
        from ..executor.common import load_settings
        self._executor_settings = load_settings(self.root, loaded, document=settings)
        stt = settings.get("stt", {})
        self.backend_name = backend_name or stt.get("backend", "moonshine")
        self.device = device or stt.get("device", "cpu")
        self.execution_timeout, self.recognition_timeout = execution_timeout, recognition_timeout
        self.startup_timeout, self.frame_timeout = startup_timeout, frame_timeout
        self.cleanup_timeout = cleanup_timeout
        self._backend_factory = backend_factory
        self._microphone_factory = microphone_factory or SelectedInput
        self._segmenter_factory = segmenter_factory
        self._executor = executor
        self._available = available
        self._devices = devices or input_devices
        self._foreground_app = foreground_app
        from ..browser_context import BrowserSession
        self._browser_session = BrowserSession()
        self._screen_target = None
        from ..screen import vision_settings
        from ..screen_frames import ScreenFrames
        self.screen_frames = ScreenFrames()
        self._vision_settings = vision_settings(settings)
        self._vision_selector = None
        self._text_selector = None
        self._selector_lock = threading.Lock()
        self._selector_state = threading.Lock()
        self._selector_worker = None
        self._selector_pending = None
        self._selector_epoch = 0
        self._retired_selectors = []
        self._screen_error = None
        self._backend_lock = threading.Lock()
        self._backend = self._segmenter = None
        self._reload_token = 0
        self._config_generation = 0
        self._model_requests = queue.Queue(maxsize=1)
        self._initial_settings = settings
        self._resolver_settings = None
        self.events, self._commands = queue.Queue(), queue.Queue()
        self._audio, self._segments = queue.Queue(maxsize=64), queue.Queue(maxsize=8)
        self._partial_results = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread = None
        self.closed = threading.Event()
        self.ready = threading.Event()
        self._enabled = self._holding = self._hold_pending = self._suspended = False
        self._stream_open = False
        self._fatal = False
        self._microphone_index = None
        self._capture_thread = self._capture_stop = None
        self._capture_closed = threading.Event()
        self._capture_closed.set()
        self._capture_started = self._last_frame = self._capture_stopping = None
        self._capture_failure_reported = False
        self._capture_generation = None
        self._release_hold = False
        self._recognition_started = None
        self._action_cancel = None
        self._action_lock = threading.Lock()
        self._activity_state = None
        self._log = DecisionLog(self.root)
        self.session = GateSession(configured_gate(self.root, loaded), execute=self._execute,
                                   log=self._session_row, foreground_app=self._foreground,
                                   grounded_resolver=self._grounded,
                                   context_reset=self._reset_context)
        self.session.control(enabled=False)

    def _submit(self, command, value=None):
        if command in {"close", "suspend", "microphone", "toggle_enabled"} or (
                command == "enabled" and not value):
            self.screen_frames.clear()
        if not self.closed.is_set():
            self._commands.put((command, value))

    def _reset_context(self):
        self._browser_session.clear()
        self.screen_frames.clear()

    def screen_capture_state(self):
        # The GUI reads these atomic state fields without waiting on recognition.
        generation = self.session.generation
        # Hold-to-talk closes audio before recognition. Its one pending command
        # still needs fresh pixels until hear/execute returns and hold_done runs.
        # A revoked capture generation must never prolong that authorization.
        pending_hold = self._hold_pending and self._capture_generation == generation
        enabled = (self._vision_settings["enabled"] and (self._stream_open or pending_hold)
                   and self.session.capture_allowed and not self.session.gate.asleep
                   and not self._suspended and not self._stop.is_set() and not self._fatal)
        self.screen_frames.set_enabled(enabled, generation)
        return enabled, generation

    def screen_capture_error(self, detail):
        if detail != self._screen_error:
            self._screen_error = detail
            if detail:
                self._emit(event="error", detail=detail)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="voicebar-runtime", daemon=True)
            self._thread.start()

    def set_enabled(self, enabled):
        self._submit("enabled", bool(enabled))

    def toggle_enabled(self):
        self._submit("toggle_enabled")

    def set_mode(self, mode):
        if mode not in {"name", "open"}:
            raise ValueError("Mode must be name or open")
        self._submit("mode", mode)

    def set_microphone(self, index):
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError("Microphone must be a nonnegative index or None")
        self._submit("microphone", index)

    def hold_start(self):
        self._submit("hold_start")

    def hold_end(self):
        self._submit("hold_end")

    def suspend(self):
        self._submit("suspend", True)

    def resume(self):
        self._submit("suspend", False)

    def close(self):
        self._submit("close")

    def refresh_microphones(self):
        self._submit("microphones")

    def _watch_config(self):
        from ..configuration import ConfigReloader
        try:
            watcher = ConfigReloader(self.root, self.loaded)
            while not self._stop.wait(1):
                result = watcher.poll()
                if result is not None:
                    self._submit("config", result)
        except (RuntimeError, ValueError, OSError) as error:
            self._emit(event="error", detail=f"Config watcher stopped: {error}")

    def _request_model(self, settings):
        self.ready.clear()
        self._retire_selectors()
        self._reload_token += 1
        while True:
            try:
                self._model_requests.get_nowait()
            except queue.Empty:
                break
        self._model_requests.put_nowait((self._reload_token, self.backend_name, self.device,
                                        self.loaded, settings, tuple(self.session.gate.names)))

    def _apply_config(self, result):
        if not result.ok:
            self._emit(event="error", detail=result.error)
            return
        from .settings import load_ui_settings
        try:
            gate = configured_gate(self.root, result.loaded, document=result.settings)
            ui = load_ui_settings(self.root, document=result.settings)
        except (RuntimeError, ValueError, OSError) as error:
            self._emit(event="error", detail=f"Config was not reloaded: {error}")
            return
        self._cancel()
        self._stop_capture()
        self._holding = self._hold_pending = False
        self._config_generation += 1
        self._retire_selectors()
        with self.session.lock:
            previous = self.session.gate
            gate.asleep, gate.dictating = previous.asleep, previous.dictating
            gate.last_app = previous.last_app if previous.last_app in gate.catalog.apps else None
            self.loaded, self.session.gate = result.loaded, gate
        self.execution_timeout = ui["execution_timeout_seconds"]
        from ..executor.common import load_settings
        self._executor_settings = load_settings(self.root, self.loaded, document=result.settings)
        from ..screen import vision_settings
        self._vision_settings = vision_settings(result.settings)
        self.screen_frames.clear()
        stt = result.settings.get("stt", {})
        choice = stt.get("backend"), stt.get("device", "cpu")
        if choice != (self.backend_name, self.device) or any(
                name != "settings.yaml" for name in result.changed_files) or (
                result.settings.get("audio") != self._initial_settings.get("audio")):
            self.backend_name, self.device = choice
            self._request_model(result.settings)
        self._initial_settings = result.settings
        if self.ready.is_set():
            self._configure_resolver(result.settings)
        else:
            from ..resolver import resolve
            self.session.resolver = resolve
            self._resolver_settings = None
        self._emit(event="config_reloaded", settings=ui, changed_files=result.changed_files,
                   detail="Config reloaded; queued speech was discarded")

    def _configure_resolver(self, document):
        from ..resolver import configured_resolver

        config = document.get("resolver", {})
        if config == self._resolver_settings:
            return
        if self._vision_settings["enabled"] or config.get("model") is not None:
            # No text retry can race the earlier vision warmup. The managed
            # callable starts retries only after its ordered warm is attempted.
            replacement = configured_resolver(self.root, document=document, preload=False)
        else:
            replacement = configured_resolver(self.root, document=document)
        self._text_selector = replacement
        with self.session.lock:
            self.session.resolver = replacement
            self._resolver_settings = config
        self._prepare_vision()

    def _retire_selectors(self):
        """Revoke old requests without waiting on the controller/UI thread."""
        from ..resolver import resolve

        with self._selector_state:
            self._selector_epoch += 1
            self._selector_pending = None
            for selector in (self._text_selector, self._vision_selector):
                if selector is not None:
                    stop = getattr(selector, "stop", None)
                    if callable(stop):
                        stop()
                    self._retired_selectors.append(selector)
            self._text_selector = self._vision_selector = None
            # Completed handles own no work. Retain only actual in-flight
            # requests so repeated settings changes cannot grow this list.
            self._retired_selectors = [selector for selector in self._retired_selectors
                if callable(getattr(selector, "wait_idle", None)) and not selector.wait_idle(0)]
        self._resolver_settings = None
        self.session.resolver = resolve

    def _prepare_speech_models(self, token, device, document):
        """Quiesce old work and release configured CUDA occupants before ASR."""
        from ..resolver import release_configured_models

        deadline = time.monotonic() + 10.0

        def remaining():
            if self._stop.is_set() or token != self._reload_token:
                raise RuntimeError("Model preparation was cancelled")
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Local selector preparation timed out")
            return budget

        if not self._selector_lock.acquire(timeout=remaining()):
            raise TimeoutError("Previous selector warmup did not finish; speech reload refused")
        try:
            # The controller may append more retired handles after a newer
            # config arrives. Drain only this snapshot, then recheck the token.
            with self._selector_state:
                retired = list(self._retired_selectors)
            for selector in retired:
                wait_idle = getattr(selector, "wait_idle", None)
                if callable(wait_idle) and not wait_idle(remaining()):
                    raise TimeoutError("Previous selector request did not finish; speech reload refused")
            remaining()
            if device == "cuda":
                release_configured_models(document, timeout_s=remaining(),
                    cancelled=lambda: self._stop.is_set() or token != self._reload_token)
            remaining()
            with self._selector_state:
                self._retired_selectors = [selector for selector in self._retired_selectors
                                           if all(selector is not old for old in retired)]
        finally:
            self._selector_lock.release()

    def _prepare_vision(self):
        """Optional model preload after speech loads; never block the GUI."""
        from ..resolver.vision import VisionSelector
        config = self._vision_settings
        if self._vision_selector is not None:
            return
        selector = VisionSelector(config["model"], port=config["port"]) if config["enabled"] else None
        self._vision_selector = selector
        if selector is None and not callable(getattr(self._text_selector, "warm", None)):
            return
        with self._selector_state:
            self._selector_pending = (self._selector_epoch, selector, self._text_selector)
            if self._selector_worker is not None:
                return
            worker = threading.Thread(target=self._warm_selectors, name="voicebar-selector-preload", daemon=True)
            self._selector_worker = worker
            try:
                worker.start()
            except Exception:  # noqa: BLE001 - tier 1 remains available without optional preload.
                self._selector_worker = self._selector_pending = None
                self._emit(event="error", detail="Local selector preload could not start")

    def _warm_selectors(self):
        """One worker and one replaceable job, including during rapid reloads."""
        while True:
            with self._selector_state:
                job, self._selector_pending = self._selector_pending, None
                if job is None or self._stop.is_set():
                    self._selector_worker = None
                    return
            epoch, selector, text_selector = job
            try:
                with self._selector_lock:
                    if self._stop.is_set() or epoch != self._selector_epoch:
                        continue
                    # A timed-out selector may still own its transport worker.
                    # A newer preload must wait for that allocation to finish.
                    with self._selector_state:
                        retired = list(self._retired_selectors)
                    deadline = time.monotonic() + self.cleanup_timeout
                    if any(not old.wait_idle(max(0, deadline - time.monotonic())) for old in retired):
                        if epoch == self._selector_epoch and not self._stop.is_set():
                            self._emit(event="error", detail="Local selector cleanup is pending; preload stopped")
                        continue
                    with self._selector_state:
                        self._retired_selectors = [old for old in self._retired_selectors
                            if all(old is not finished for finished in retired)]
                    if self._stop.is_set() or epoch != self._selector_epoch:
                        continue
                    if selector is not None:
                        ready = selector.warm(timeout_s=60)
                        if not selector.wait_idle(self.cleanup_timeout):
                            if epoch == self._selector_epoch and not self._stop.is_set():
                                self._emit(event="error", detail="Local vision cleanup is pending; selector preload stopped")
                            continue
                        if self._stop.is_set() or epoch != self._selector_epoch:
                            continue
                        if not ready:
                            self._emit(event="error", detail="Local vision is unavailable; exact-label commands still work")
                    prepare_text = getattr(text_selector, "warm", None)
                    if callable(prepare_text):
                        prepare_text()
            except Exception as error:  # noqa: BLE001 - optional selector failures do not disable tier 1.
                if epoch == self._selector_epoch and not self._stop.is_set():
                    self._emit(event="error", detail=f"Local selector preload failed: {type(error).__name__}")

    def _close_model(self, backend):
        try:
            backend.close()
            return True
        except BaseException as error:  # noqa: BLE001 - native cleanup failure must revoke execution
            detail = f"Speech model cleanup failed; restart Voicebar: {error}"
            if self._stop.is_set():
                self._emit(event="error", detail=detail)
            else:
                self._fail(detail)
            return False

    def _emit(self, **row):
        if row.get("event") == "activity" and row.get("state") == self._activity_state:
            return
        self._activity_state = row.get("state")
        defaults = {"enabled": self._enabled, "stream_open": self._stream_open,
                    "dictating": self.session.gate.dictating,
                    "mode": self.session.gate.mode,
                    "state": self._state()}
        if getattr(self.session.gate, "conversational", False):
            defaults["conversation_active"] = (time.perf_counter() <= self.session.gate.until
                                                and not self.session.gate.asleep)
            defaults["conversation_enabled"] = True
        self.events.put({**defaults, **row})

    def _state(self):
        if self._suspended or self.session.locked or not self._stream_open:
            return "off"
        return "dictation" if self.session.gate.dictating else "asleep" if self.session.gate.asleep else "idle"

    def _session_row(self, row):
        self._log(row)
        if row["event"] != "decision":
            self._emit(**{key: value for key, value in row.items() if key != "state"})
            return
        reason = row["reason"]
        results = row.get("results", [])
        ok = reason == "run" and bool(results) and all(result.get("ok") for result in results)
        state = "done" if ok else "not sure"
        detail = row.get("detail") or "; ".join(result.get("detail", "") for result in results)
        candidates = row.get("candidates", []) + [
            candidate for result in results for candidate in result.get("candidates", [])]
        if not detail and reason in {"ignored", "unresolved"}:
            _, body = self.session.gate._split_name(row["text"])
            explanation = explain_failure(body, self.loaded.catalog)
            for target in explanation["targets"]:
                candidates = target["candidates"][:2]
                detail = f"Unknown or ambiguous target: {target['query']}"
                break
        detail = detail or {"armed": "Ready for a command", "asleep": "Asleep; say the wake name and wake up",
                            "pending": "Listening for the rest of that command…",
                            "cancelled": "Cancelled",
                            "ignored": "Wake name required or command was not allowed",
                            "unresolved": "Command was not recognized",
                            "stale_or_closed": "Discarded because listening authorization changed",
                            "chime_overlap": "Discarded because audio overlapped a cue"}.get(reason, reason)
        if self.session.gate.dictating and ok:
            state = "dictation"
        elif self.session.gate.asleep and ok:
            state = "asleep"
        self._emit(event="decision", state=state, text=row["text"], detail=detail,
                   candidates=candidates, latency_ms=row["timings"].get("end_to_end_ms"),
                   reason=reason, plan=row.get("plan"), results=results)

    def _foreground(self):
        self._screen_target = None
        if (not getattr(self.session.gate, "conversational", False)
                and not self.loaded.catalog.app_actions):
            return None
        if self._foreground_app is not None:
            return self._foreground_app()
        from ..executor.common import Context
        if not self.loaded.catalog.apps:
            return None
        with Context(self.root, loaded=self.loaded, persist=False,
                     settings=self._executor_settings) as context:
            hwnd = context.native.foreground()
            windows = [window for window in context.native.windows() if window.hwnd == hwnd]
            if len(windows) != 1:
                return None
            window = windows[0]
            name = context.apps.name_for_window(window)
            if (name is None or context.native.foreground() != hwnd
                    or not context.native.is_window(hwnd, window.pid)):
                return None
            gate = self.session.gate
            remembering = (not getattr(gate, "conversational", False)
                           or (not gate.asleep and time.perf_counter() <= gate.until))
            if remembering and context.apps.browser(window):
                self._browser_session.observe_caption(window)
            return name

    def _grounded(self, text):
        if not self.ready.is_set():
            return None
        from ..screen import resolve_control
        config = self._vision_settings
        if config["enabled"] and self._vision_selector is None:
            from ..resolver.vision import VisionSelector
            self._vision_selector = VisionSelector(config["model"], port=config["port"])
        selector, epoch = self._vision_selector, self._selector_epoch
        selection = SimpleNamespace(last_outcome="not_started")

        def select(*args, **kwargs):
            # Exact UIA matching can run while a model warms. Only inference
            # participates in the allocation lifecycle and must fail fast.
            if not self._selector_lock.acquire(blocking=False):
                selection.last_outcome = "busy"
                return None
            try:
                if (not self.ready.is_set() or self._stop.is_set()
                        or epoch != self._selector_epoch or selector is not self._vision_selector):
                    selection.last_outcome = "not_sure"
                    return None
                result = selector.select(*args, **kwargs)
                selection.last_outcome = getattr(selector, "last_outcome", "not_sure")
                return result
            finally:
                self._selector_lock.release()

        selection.select = select

        generation = self.session.generation
        result = resolve_control(
            text, self.root, self.loaded, settings=self._executor_settings,
            frames=self.screen_frames if config["enabled"] else None,
            selector=selection if config["enabled"] else None,
            generation=generation, timeout_s=config["timeout_s"],
            on_vision=lambda: self._emit(event="activity", state="working",
                                        detail="Understanding the current screen…"),
            authorized=lambda: (generation == self.session.generation
                                and self.session.capture_allowed
                                and not self.session.gate.asleep))
        if result is None:
            return None
        plan, self._screen_target = result
        return plan

    def _cancel(self):
        with self._action_lock:
            if self._action_cancel is not None:
                self._action_cancel.set()
        self.session.invalidate()

    def _fail(self, detail):
        self._commands.put(("fatal", detail))

    def _execute(self, plan):
        cancel, result = threading.Event(), queue.Queue(maxsize=1)
        with self._action_lock:
            self._action_cancel = cancel
        generation = self.session.generation

        def run():
            try:
                if cancel.is_set() or generation != self.session.generation or not self.session.capture_allowed:
                    value = [{"ok": False, "detail": "Execution authorization revoked"}]
                elif self._executor is not None:
                    value = self._executor(plan, cancel)
                else:
                    from ..executor import Context, execute
                    authorization = SimpleNamespace(is_set=lambda: (
                        cancel.is_set() or generation != self.session.generation
                        or not self.session.capture_allowed))
                    with Context(self.root, loaded=self.loaded, cancel=authorization,
                                 settings=self._executor_settings,
                                 remember_app=not getattr(self.session.gate, "conversational", False),
                                 monitor_reference=self.session.monitor_reference) as context:
                        context.browser_session = self._browser_session
                        context.required_foreground = self._screen_target
                        execute_entered = time.perf_counter()
                        value = execute(plan, context=context)
                    for item in value:
                        if item.get("queued_ms") is not None:
                            item["action_started_at"] = execute_entered + item["queued_ms"] / 1000
                result.put((True, value))
            except BaseException as error:  # noqa: BLE001 - transfer all action worker failures
                result.put((False, error))

        thread = threading.Thread(target=run, name="voicebar-action", daemon=True)
        thread.start()
        try:
            success, value = result.get(timeout=self.execution_timeout)
        except queue.Empty:
            cancel.set()
            self.session.invalidate("execution_timeout")
            self._fail("Action timed out. Listening is Off; restart Voicebar before another action.")
            return [{"ok": False, "detail": "Action timed out; outcome unknown. No further actions will run; restart Voicebar.",
                     "timed_out": True, "outcome_unknown": True}]
        finally:
            with self._action_lock:
                if self._action_cancel is cancel:
                    self._action_cancel = None
        if not success:
            return [{"ok": False, "detail": str(value)}]
        return value

    def _load(self):
        from ..audio import SpeechSegmenter
        from ..endpoint import EndpointSettings, PartialPolicy
        from ..stt import load_backend

        try:
            while not self._stop.is_set():
                try:
                    token, name, device, loaded, document, names = self._model_requests.get(timeout=.05)
                except queue.Empty:
                    continue
                if token != self._reload_token or self._fatal:
                    continue
                backend = None
                try:
                    self._prepare_speech_models(token, device, document)
                    # Only this worker disposes models, serialized against
                    # recognition. Keep one GPU allocation at a time.
                    with self._backend_lock:
                        previous, self._backend = self._backend, None
                        if previous is not None and not self._close_model(previous):
                            continue
                    if name is None:
                        raise RuntimeError("No speech backend selected. Test a WAV and set stt.backend in config/settings.yaml.")
                    backend = (self._backend_factory or load_backend)(name, self.root, loaded.catalog, device=device)
                    if self._stop.is_set() or token != self._reload_token:
                        self._close_model(backend)
                        backend = None
                        continue
                    warmup = getattr(backend, "warmup", None)
                    if callable(warmup):
                        warmup()
                    if self._stop.is_set() or token != self._reload_token:
                        self._close_model(backend)
                        backend = None
                        continue
                    endpoint = EndpointSettings.from_document(document)
                    policy = PartialPolicy(loaded.catalog, names=names)
                    if self._segmenter_factory is None:
                        segmenter = SpeechSegmenter(
                            root=self.root, silence_seconds=endpoint.silence_seconds,
                            short_silence_seconds=endpoint.short_silence_seconds if endpoint.adaptive else None,
                            partial_policy=policy if endpoint.adaptive else None)
                    else:
                        segmenter = self._segmenter_factory()
                    with self._backend_lock:
                        if self._stop.is_set() or token != self._reload_token:
                            self._close_model(backend)
                            backend = None
                            continue
                        self._backend, self._segmenter = backend, segmenter
                    self._submit("loaded", (token, backend, segmenter))
                except BaseException as error:  # noqa: BLE001 - preserve native initialization failures
                    if backend is not None:
                        self._close_model(backend)
                    if token == self._reload_token:
                        self._submit("model_failed", (token, str(error)))
        finally:
            # Shutdown never races the controller closing a still-loading
            # model. A native load/recognition that hangs leaves this daemon
            # visibly alive; disposal follows when that operation returns.
            with self._backend_lock:
                previous, self._backend = self._backend, None
                if previous is not None:
                    self._close_model(previous)

    def _enumerate(self):
        try:
            self._emit(event="microphones", devices=self._devices(), microphone=self._microphone_index)
        except Exception as error:  # noqa: BLE001 - tray enumeration must fail visibly
            self._emit(event="microphones", devices=[], detail=f"Microphone enumeration failed: {error}")

    def _stop_capture(self, *, release=False):
        if self._capture_stop is not None:
            self._release_hold = release
            if self._capture_stopping is None:
                self._capture_stopping = time.perf_counter()
            self._capture_stop.set()

    def _start_capture(self):
        generation, holding = self.session.generation, self._holding
        stop = self._capture_stop = threading.Event()
        self._capture_closed.clear()
        self._capture_generation = generation
        self._capture_started = time.perf_counter()
        self._last_frame = self._capture_stopping = None
        self._capture_failure_reported = False
        self._release_hold = False
        microphone = self._microphone_factory(self._microphone_index)

        def capture():
            origin, count = None, 0

            def receive(pcm, frames, status):
                nonlocal origin, count
                if stop.is_set() or self._stop.is_set() or generation != self.session.generation:
                    return
                if status or type(pcm) is not bytes or not 0 < frames <= 512 or len(pcm) != frames * 2:
                    self._fail(f"Invalid microphone frame: {status or 'PCM shape'}")
                    stop.set()
                    return
                from ..stt.base import SAMPLE_RATE
                self._last_frame = time.perf_counter()
                if origin is None:
                    origin = self._last_frame - frames / SAMPLE_RATE
                at = origin + count / SAMPLE_RATE
                count += frames
                try:
                    self._audio.put_nowait((generation, holding, pcm, at))
                except queue.Full:
                    self._fail("Audio backlog exceeded its bound; incomplete speech was discarded")
                    stop.set()

            try:
                with microphone.open(receive) as info:
                    self._stream_open = True
                    self.session.event("stream_opened", stream_open=True, device=info)
                    stop.wait()
                if microphone.is_open:
                    raise RuntimeError("Microphone adapter did not confirm closure")
                self._stream_open = False
                self.session.event("stream_closed", stream_open=False)
                if holding and self._release_hold and generation == self.session.generation:
                    try:
                        self._audio.put_nowait((generation, True, None, time.perf_counter()))
                    except queue.Full:
                        self._fail("Hold recording backlog exceeded its bound; command discarded")
            except BaseException as error:  # noqa: BLE001 - transfer native capture/closure failures
                self._fail(f"Microphone failed: {error}")
            finally:
                self._capture_closed.set()

        self._capture_thread = threading.Thread(target=capture, name="voicebar-capture", daemon=True)
        self._capture_thread.start()

    def _segment(self, segmenter):
        import numpy as np

        from ..audio import SpeechSegment
        from ..stt.base import SAMPLE_RATE

        previous, held, held_start, held_count = None, [], None, 0
        try:
            while not self._stop.is_set():
                try:
                    generation, holding, pcm, at = self._audio.get(timeout=.05)
                except queue.Empty:
                    continue
                if generation != self.session.generation or not self.session.capture_allowed:
                    continue
                if self._segmenter is not None:
                    segmenter = self._segmenter
                if previous != generation:
                    segmenter.reset()
                    held, held_start, held_count, previous = [], None, 0, generation
                if holding:
                    if pcm is None:
                        if held:
                            recording = np.concatenate(held)
                            # Match the existing PTT silence guard. A recognizer's
                            # hallucination on silent input is never a command.
                            if float(np.sqrt(np.mean(recording ** 2))) < .001:
                                self._emit(event="activity", state="not sure", detail="No speech in hold recording")
                                self._commands.put(("hold_done", generation))
                                held, held_count = [], 0
                                continue
                            block = SpeechSegment(recording, held_start,
                                                  held_start + held_count / SAMPLE_RATE, at)
                            self._segments.put_nowait((generation, block, True))
                        else:
                            self._commands.put(("hold_done", generation))
                        held, held_count = [], 0
                        continue
                    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                    held_start = at if held_start is None else held_start
                    held_count += len(audio)
                    if held_count > SAMPLE_RATE * 20:
                        raise RuntimeError("Hold-to-talk reached its 20 second cap; incomplete command discarded")
                    held.append(audio)
                    self._emit(event="activity", state="hearing", detail="Hold-to-talk recording")
                    continue
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                while True:
                    try:
                        partial_generation, preview, text = self._partial_results.get_nowait()
                    except queue.Empty:
                        break
                    if partial_generation == generation and hasattr(segmenter, "update_partial"):
                        policy = segmenter._partial_policy
                        if policy is not None:
                            policy.dictating = self.session.gate.dictating
                            policy.last_app = self.session.gate.last_app
                            policy.names = tuple(self.session.gate.names)
                            policy.catalog = self.session.gate.catalog
                        segmenter.update_partial(text, start=preview.start, speech_end=preview.speech_end)
                before = segmenter.last_speech_at
                blocks = segmenter.feed(audio, at)
                if segmenter.last_speech_at is not None:
                    self.session.speech(segmenter.last_speech_at)
                    if before != segmenter.last_speech_at:
                        self._emit(event="activity", state="hearing")
                for drop in segmenter.drain_drops():
                    self._log({"event": "audio_drop", "reason": drop.reason, "start": drop.start, "end": drop.end})
                    self._emit(event="activity", state="not sure", detail=f"Speech discarded: {drop.reason}")
                for block in blocks:
                    self._segments.put_nowait((generation, block, False))
                if hasattr(segmenter, "drain_previews"):
                    for preview in segmenter.drain_previews():
                        self._segments.put_nowait((generation, preview, False))
        except BaseException as error:  # noqa: BLE001 - transfer audio worker failures
            self._fail(f"Audio segmentation stopped: {error}")

    def _recognize(self, backend):
        from ..audio import SpeechPreview
        from ..stt.base import transcribe_segment

        try:
            while not self._stop.is_set():
                try:
                    generation, block, held = self._segments.get(timeout=.05)
                except queue.Empty:
                    continue
                if generation != self.session.generation or not self.session.capture_allowed:
                    continue
                if isinstance(block, SpeechPreview) and (self._segmenter is None or
                        not self._segmenter.preview_is_current(block)):
                    continue
                events = []
                entered = self._recognition_started = time.perf_counter()
                self._emit(event="activity", state="working")
                with self._backend_lock:
                    if (generation != self.session.generation or self._backend is None
                            or not self.session.capture_allowed):
                        self._recognition_started = None
                        continue
                    transcribe_segment(self._backend, block.audio, events.append)
                self._recognition_started = None
                finals = {event.line_id: event for event in events if event.final}
                text = " ".join(event.text.strip() for event in sorted(
                    finals.values(), key=lambda event: (event.start_sample, event.line_id)) if event.text.strip())
                if isinstance(block, SpeechPreview):
                    self._partial_results.put_nowait((generation, block, text))
                    continue
                self.session.hear(text, start=block.start, end=block.end,
                                  speech_end=block.speech_end, generation=generation,
                                  push_to_talk=held, timings={
                                      "stt_ms": (time.perf_counter() - entered) * 1000,
                                      "queue_ms": max(0.0, (entered - block.end) * 1000),
                                      "endpoint_ms": max(0.0, (block.end - block.speech_end) * 1000)})
                if held:
                    self._commands.put(("hold_done", generation))
        except BaseException as error:  # noqa: BLE001 - transfer recognition worker failures
            self._fail(f"Speech recognition stopped: {error}")

    def _run(self):
        backend, workers = None, []
        loader = threading.Thread(target=self._load, name="voicebar-model-loader", daemon=True)
        self._emit(event="loading", state="off", detail="Loading local speech model; microphone is Off")
        self._request_model(self._initial_settings)
        loader.start()
        threading.Thread(target=self._enumerate, name="voicebar-microphones", daemon=True).start()
        threading.Thread(target=self._watch_config, name="voicebar-config", daemon=True).start()
        if self._available is None:
            from ..ptt import WindowsHotkey, parse_hotkey
            desktop = WindowsHotkey(parse_hotkey("ctrl+alt+f8"))
            self._available = desktop.available  # no registration; GUI owns all hotkeys
        try:
            while not self._stop.is_set():
                try:
                    command, value = self._commands.get(timeout=.02)
                except queue.Empty:
                    command, value = None, None
                if command == "loaded":
                    token, backend, segmenter = value
                    if token != self._reload_token:
                        continue
                    # Let speech reserve its GPU memory before Ollama chooses
                    # an offload budget. Preloading still runs off the UI thread.
                    self._configure_resolver(self._initial_settings)
                    if not workers:
                        workers = [threading.Thread(target=self._segment, args=(segmenter,), name="voicebar-audio", daemon=True),
                               threading.Thread(target=self._recognize, args=(backend,), name="voicebar-recognition", daemon=True)]
                        for worker in workers:
                            worker.start()
                    self.ready.set()
                    detail = "Ready" if self.loaded.pinned else "Ready. Add exact app names to config/pinned.yaml before using open mode."
                    self._emit(event="ready", detail=detail, log=str(self._log.path))
                elif command == "config":
                    self._apply_config(value)
                elif command == "model_failed" and value[0] == self._reload_token:
                    self.ready.clear()
                    self._enabled = self._holding = self._hold_pending = False
                    self._cancel()
                    self.session.control(enabled=False)
                    self._stop_capture()
                    self._emit(event="error", state="off", detail=value[1])
                elif command == "close":
                    self._enabled = self._holding = self._hold_pending = False
                    self._cancel()
                    self.session.control(enabled=False)
                    self._stop_capture()
                    self._stop.set()
                    break
                elif command == "fatal":
                    self._fatal = True
                    self._enabled = self._holding = self._hold_pending = False
                    self._cancel()
                    self.session.control(enabled=False)
                    self._stop_capture()
                    self._emit(event="error", state="off", detail=value)
                elif command in {"enabled", "toggle_enabled", "mode", "microphone", "suspend"}:
                    self._cancel()
                    self._holding = self._hold_pending = False
                    if command in {"enabled", "toggle_enabled"}:
                        self._enabled = (not self._enabled if command == "toggle_enabled" else bool(value)) and not self._fatal
                    elif command == "mode":
                        self.session.gate.mode = value
                    elif command == "microphone":
                        self._microphone_index = value
                    else:
                        self._suspended = value
                    self.session.control(enabled=self._enabled, locked=self._suspended or not self._available())
                    self._stop_capture()
                    self._emit(event="control", microphone=self._microphone_index)
                elif command == "hold_start" and not self._fatal and not self._suspended and self._available():
                    if not self._holding and not self._hold_pending:
                        self._cancel()
                        self._holding = True
                        self.session.control(enabled=True, locked=False)
                        self._stop_capture()
                elif command == "hold_end" and self._holding:
                    self._holding = False
                    # A pending stream from before hold_start is not a hold.
                    active = self._capture_thread is not None and self._capture_generation == self.session.generation
                    self._hold_pending = active
                    self._stop_capture(release=active)
                    if not active:
                        self.session.control(enabled=self._enabled)
                elif command == "hold_done" and value == self.session.generation:
                    self._hold_pending = False
                    self.session.invalidate("hold_done", clear_followup=False)
                    self.session.control(enabled=self._enabled)
                elif command == "microphones":
                    threading.Thread(target=self._enumerate, name="voicebar-microphones", daemon=True).start()
                locked = self._suspended or not self._available()
                if locked and not self.session.locked:
                    self._holding = self._hold_pending = False
                    self._cancel()
                    self.session.control(enabled=self._enabled)
                self.session.control(locked=locked)
                self.session.tick()
                now = time.perf_counter()
                if self._recognition_started is not None and now - self._recognition_started > self.recognition_timeout:
                    self._recognition_started = None
                    self._fail("Recognition timed out. Listening is Off; restart Voicebar.")
                allowed = self.ready.is_set() and not self._fatal and self.session.capture_allowed and not self._hold_pending
                if self._capture_thread is not None:
                    if not allowed or self._capture_generation != self.session.generation:
                        self._stop_capture(release=self._release_hold)
                    if self._capture_closed.is_set():
                        self._capture_thread = self._capture_stop = None
                        self._capture_stopping = None
                    elif (self._capture_stopping is not None and not self._capture_failure_reported
                          and now - self._capture_stopping > self.cleanup_timeout):
                        self._capture_failure_reported = True
                        self._fail("Microphone closure could not be confirmed. Restart Voicebar.")
                    elif self._capture_stop is not None and not self._capture_stop.is_set():
                        if self._last_frame is None and now - self._capture_started > self.startup_timeout:
                            self._fail("Microphone startup timed out; listening was turned Off")
                        elif self._last_frame is not None and now - self._last_frame > self.frame_timeout:
                            self._fail("Microphone frame deadline expired; listening was turned Off")
                if allowed and self._capture_thread is None:
                    self._start_capture()
        except BaseException as error:  # noqa: BLE001 - revoke authorization on controller failure
            self._cancel()
            self.session.control(enabled=False)
            self._stop_capture()
            self._emit(event="error", state="off", detail=f"Runtime stopped: {error}")
        finally:
            self._stop.set()
            self._retire_selectors()
            self._stop_capture()
            if self._capture_thread is not None:
                self._capture_thread.join(self.cleanup_timeout)
            for worker in workers:
                worker.join(self.cleanup_timeout)
            # The persistent loader normally exits within its 50 ms queue
            # deadline. Do not make a hung native load hold the GUI open.
            loader.join(min(self.cleanup_timeout, .1))
            with self._selector_state:
                selector_worker = self._selector_worker
            if selector_worker is not None:
                selector_worker.join(min(self.cleanup_timeout, .1))
            alive = [worker.name for worker in workers if worker.is_alive()]
            if selector_worker is not None and selector_worker.is_alive():
                alive.append(selector_worker.name)
            if self._capture_thread is not None and self._capture_thread.is_alive():
                alive.append(self._capture_thread.name)
            if loader.is_alive():
                # A daemon loader may still own a native initialization call.
                # It must not be reused in this process after GUI shutdown.
                alive.append(loader.name)
            try:
                self._log({"event": "runtime_stopped", "stream_open": self._stream_open, "workers_alive": alive})
            except (OSError, ValueError, RuntimeError) as error:
                self._emit(event="error", detail=f"Final decision log could not be written: {error}")
            finally:
                self._emit(event="stopped", state="off", workers_alive=alive)
                self.closed.set()
