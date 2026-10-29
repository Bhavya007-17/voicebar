"""Always-on local audio, gate decisions, and observable microphone lifecycle."""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import Counter, deque
from copy import deepcopy
from pathlib import Path

from .app_gate import Stage7Gate
from .catalog import CatalogError, _yaml_file
from .gate import Gate
from .logs import DecisionLog
from .parser import tokenize
from .schema import validate_plan

_UNSAFE = threading.Event()


class ListeningFailure(RuntimeError):
    """A stopped listener with retained decisions and explicit cleanup evidence."""

    def __init__(self, session, log_path, outcome, cause):
        self.session, self.log_path = session, log_path
        self.outcome, self.cause = outcome, cause
        details = "; ".join(f"{row['phase']}: {row['message']}" for row in outcome["failures"])
        super().__init__(details or str(cause))


def configured_gate(root, loaded, document=None) -> Gate:
    document = _yaml_file(Path(root) / "config/settings.yaml") if document is None else document
    if not isinstance(document, dict):
        raise CatalogError("settings.yaml must be a mapping")
    names = document.get("names", [document.get("name", "computer")])
    if (not isinstance(names, list) or not names
            or any(not isinstance(n, str) or len(tokenize(n)) != 1
                   or tokenize(n)[0].norm != n.lower() for n in names)):
        raise CatalogError("settings.yaml names must be a nonempty list of exact single words")
    mode = document.get("mode", "name")
    if mode not in ("name", "open"):
        raise CatalogError("settings.yaml mode must be name or open")
    seconds = document.get("followup_seconds", 8)
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or not 0 <= seconds <= 3600):
        raise CatalogError("followup_seconds must be finite and from 0 to 3600")
    conversation = document.get("conversation", {})
    if not isinstance(conversation, dict) or set(conversation) - {
            "enabled", "silence_seconds", "continuation_seconds"}:
        raise CatalogError("conversation must contain only enabled, silence_seconds and continuation_seconds")
    enabled = conversation.get("enabled", False)
    if type(enabled) is not bool:
        raise CatalogError("conversation enabled must be true or false")
    values = {}
    for key, default, low, high in (("silence_seconds", 120, 10, 3600),
                                    ("continuation_seconds", 4, 1, 10)):
        value = conversation.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise CatalogError(f"conversation {key} must be from {low} to {high}")
        values[key] = value
    if enabled:
        from .conversation import ConversationGate
        return ConversationGate(loaded.catalog, names=names, mode=mode, followup_s=seconds,
                                open_apps=loaded.pinned, **values)
    return Stage7Gate(loaded.catalog, names=names, mode=mode, followup_s=seconds,
                     open_apps=loaded.pinned)


class Chimes:
    """Register intervals before making sound, including a currently playing cue."""

    def __init__(self, clock=time.perf_counter, play=None):
        self.clock, self.play = clock, play or self._beep
        self.intervals = deque(maxlen=256)
        self.active = None
        self.lock = threading.Lock()

    @staticmethod
    def _beep():
        import winsound
        winsound.Beep(880, 70)

    def overlaps(self, start, end):
        with self.lock:
            return ((self.active is not None and end >= self.active)
                    or any(start <= right and end >= left for left, right in self.intervals))

    def cue(self):
        with self.lock:
            self.active = self.clock()
        try:
            self.play()
        finally:
            with self.lock:
                self.intervals.append((self.active, self.clock()))
                self.active = None


class GateSession:
    """Gate wrapper: execution completion, inactivity, Off/Locked, and UI events.

    Only this serialized recognition worker
    calls hear; the capture controller may revoke a generation at any time.
    """

    def __init__(self, gate, *, execute, dry_run=False, log=lambda row: None,
                 clock=time.perf_counter, resolver=None, chimes=None, foreground_app=None,
                 grounded_resolver=None, context_reset=None):
        from .resolver import resolve
        self.gate, self.execute, self.dry_run = gate, execute, dry_run
        self.log, self.clock = log, clock
        self.resolver, self.chimes = resolver or resolve, chimes or Chimes(clock)
        self.foreground_app = foreground_app
        self.grounded_resolver = grounded_resolver
        self.context_reset = context_reset or (lambda: None)
        self.enabled, self.locked, self.generation = True, False, 0
        self.last_speech = None
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.would_run = []
        self.decision_count = 0
        self.reason_counts = Counter()
        self.outcome = None
        self.monitor_reference = "window"
        self.report_would_run = None

    @property
    def state(self):
        if self.locked:
            return "locked"
        if not self.enabled:
            return "off"
        if self.gate.dictating:
            return "dictation"
        return "asleep" if self.gate.asleep else "listening"

    @property
    def capture_allowed(self):
        return self.enabled and not self.locked

    def event(self, reason, **extra):
        row = {"event": "state", "mode": self.gate.mode, "state": self.state,
               "reason": reason, **extra}
        self.events.put(row)
        self.log(row)

    def control(self, *, enabled=None, locked=None):
        with self.lock:
            before = self.state
            previous = (self.enabled, self.locked)
            if enabled is not None:
                self.enabled = bool(enabled)
            if locked is not None:
                self.locked = bool(locked)
            if previous == (self.enabled, self.locked):
                return
            self.generation += 1
            self.gate.until = float("-inf")
            if getattr(self.gate, "conversational", False):
                self.gate.reset_context()
                self.context_reset()
            if not self.enabled:
                self.gate.dictating = False
            self.event("control", previous=before, generation=self.generation)

    def invalidate(self, reason="authorization_changed", *, clear_followup=True):
        """Revoke queued speech without changing the requested microphone state."""
        with self.lock:
            self.generation += 1
            if clear_followup:
                self.gate.until = float("-inf")
                if getattr(self.gate, "conversational", False):
                    self.gate.reset_context()
                    self.context_reset()
            self.event(reason, generation=self.generation)

    def speech(self, at):
        with self.lock:
            if self.last_speech is None or at > self.last_speech:
                self.last_speech = at
            if getattr(self.gate, "conversational", False) and self.capture_allowed:
                self.gate.speech(at)

    def tick(self, now=None):
        with self.lock:
            at = self.clock() if now is None else now
            if getattr(self.gate, "conversational", False) and self.gate.expire(at):
                self.generation += 1
                self.context_reset()
                self.event("conversation_expired", generation=self.generation)
            if (self.gate.dictating and self.last_speech is not None
                    and at - self.last_speech >= 60):
                self.gate.dictating = False
                self.gate.until = float("-inf")
                self.generation += 1  # stale dictation must never become a command
                self.event("dictation_timeout", generation=self.generation)

    def _snapshot(self):
        return {key: getattr(self.gate, key)
                for key in ("until", "last_app", "asleep", "dictating")}

    def hear(self, text, *, start, end, speech_end, generation, timings=None,
             push_to_talk=False):
        entered = self.clock()
        timings = dict(timings or {})
        explicitly_named = self.gate._split_name(text)[0]
        conversational = getattr(self.gate, "conversational", False)
        effective_text = text
        row = {"event": "decision", "text": text, "mode": self.gate.mode,
               "state": self.state, "plan": None, "timings": timings,
               "segment_start": start, "segment_end": end, "dry_run": self.dry_run}
        with self.lock:
            self.tick()
            if generation != self.generation or not self.capture_allowed:
                row["reason"] = "stale_or_closed"
            elif self.chimes.overlaps(start, end):
                row["reason"] = "chime_overlap"
            else:
                before = self._snapshot()
                # A deliberately held hotkey authorizes exactly this utterance.
                # Keep the raw text for dictation and UI/log feedback. Asleep
                # still requires the spoken wake name, as the gate specifies.
                gate_text = text
                if (push_to_talk and not self.gate.dictating and not self.gate.asleep
                        and not self.gate._split_name(text)[0]):
                    gate_text = f"{min(self.gate.names)} {text}"
                active_app = None
                if (self.foreground_app is not None
                        and not self.gate.dictating and not self.gate.asleep):
                    try:
                        active_app = self.foreground_app()
                    except Exception as error:  # noqa: BLE001 - unknown foreground cannot authorize bare phrases.
                        row["foreground_error"] = str(error)
                row["active_app"] = active_app
                if self.foreground_app is None:
                    decision = self.gate.hear(gate_text, speech_end)
                else:
                    decision = self.gate.hear(gate_text, speech_end, active_app=active_app)
                row.update(reason=decision.reason, plan=decision.plan)
                if conversational:
                    effective_text = self.gate.last_input
                if decision.reason == "run":
                    validate_plan(decision.plan)
                    # Gate._accept runs at hear time in the immutable contract.
                    # Commit actual effects and follow-up only after execution.
                    for key, value in before.items():
                        if conversational and key == "until":
                            continue
                        setattr(self.gate, key, value)
        timings["gate_ms"] = (self.clock() - entered) * 1000
        from .parser import contains_app_phrase_prefix

        grounded_failed = False
        click_plan = (row["reason"] == "run" and len(row["plan"]) == 1
                      and row["plan"][0].get("action") == "app_action"
                      and row["plan"][0].get("app") == "active"
                      and row["plan"][0].get("name", "").startswith(("click:", "click_role:")))
        if ((row["reason"] == "unresolved" or click_plan) and conversational
                and self.grounded_resolver is not None):
            began = self.clock()
            try:
                _, body = self.gate._split_name(effective_text)
                plan = self.grounded_resolver(body)
                if plan is not None:
                    validate_plan(plan)
                    with self.lock:
                        self.tick()
                        if generation == self.generation and self.capture_allowed:
                            row.update(reason="run", plan=plan, tier="screen")
                        else:
                            row.update(reason="stale_or_closed", plan=None)
                elif click_plan:
                    # A parsed label is not an observed control. Never retry
                    # the unbound action after screen grounding found nothing.
                    grounded_failed = True
                    row.update(reason="unresolved", plan=None,
                               detail="Which control should I click? I could not find that visible label.")
            except Exception as error:  # noqa: BLE001 - unavailable controls never authorize an action
                grounded_failed = True
                row.update(reason="unresolved", plan=None)
                row["detail"] = str(error)
                row["candidates"] = getattr(error, "candidates", [])
            timings["screen_ms"] = (self.clock() - began) * 1000
        with self.lock:
            self.tick()
            if generation != self.generation or not self.capture_allowed:
                row.update(reason="stale_or_closed", plan=None)
        if (row["reason"] == "unresolved" and (explicitly_named or conversational)
                and not grounded_failed
                and not contains_app_phrase_prefix(effective_text, self.gate.catalog)):
            began = self.clock()
            try:
                resolver_text = effective_text
                if not self.gate._split_name(resolver_text)[0]:
                    resolver_text = f"{min(self.gate.names)} {resolver_text}"
                plan = self.resolver(resolver_text, self.gate.catalog, {
                    "names": self.gate.names, "last_app": before["last_app"],
                    "active_app": active_app,
                })
                if plan is not None:
                    validate_plan(plan)
            except Exception:  # noqa: BLE001 - any optional resolver failure must cause no action.
                plan = None
            timings["resolver_ms"] = (self.clock() - began) * 1000
            row["tier"] = "tier2"
            with self.lock:
                self.tick()
                if (generation != self.generation or not self.capture_allowed
                        or self.chimes.overlaps(start, end)):
                    row["reason"] = "stale_or_closed"
                elif plan is not None:
                    row.update(reason="run", plan=plan)
        elif row["reason"] == "run":
            row.setdefault("tier", "tier1")
        if row["reason"] == "run":
            from .executor.windows import monitor_reference_for
            try:
                self.monitor_reference = monitor_reference_for(text, row["plan"])
            except RuntimeError as error:
                row.update(reason="unresolved", plan=None, detail=str(error))
        if row["reason"] == "run":
            began = self.clock()
            timings["gate_entry_to_dispatch_ms"] = (began - entered) * 1000
            successful, results = [], []
            row["execution_attempted"] = False
            for action in row["plan"]:
                with self.lock:
                    self.tick()
                    if (generation != self.generation or not self.capture_allowed
                            or self.chimes.overlaps(start, end)):
                        results.append({"ok": False, "detail": "Capture authorization changed"})
                        break
                if self.dry_run or action["action"] == "mode":
                    result = {"ok": True, "detail": "dry-run" if self.dry_run else "mode"}
                else:
                    row["execution_attempted"] = True
                    try:
                        result = self.execute([action])[0]
                    except (OSError, RuntimeError, ValueError) as error:
                        result = {"ok": False, "detail": str(error)}
                results.append(result)
                started = result.get("action_started_at")
                if ("execute_start_ms" not in timings and isinstance(started, (int, float))
                        and not isinstance(started, bool) and math.isfinite(started)):
                    ready_at = entered + (timings["gate_ms"] + timings.get("resolver_ms", 0)
                                          + timings.get("screen_ms", 0)) / 1000
                    timings["execute_start_ms"] = max(0.0, (started - ready_at) * 1000)
                    timings["speech_end_to_action_start_ms"] = max(0.0, (started - speech_end) * 1000)
                if not result["ok"]:
                    break
                successful.append(action)
                if (action["action"] == "app_action"
                        and self.gate.catalog.app_actions.get(action["app"], {}).get(
                            action["name"], {}).get("then") == "sleep"):
                    successful.append({"action": "mode", "op": "sleep"})
                    row["post_action_mode"] = "sleep"
                    if len(results) < len(row["plan"]):
                        row["skipped_actions"] = row["plan"][len(results):]
                    break
            completed = self.clock()
            timings["execution_ms"] = (completed - began) * 1000
            row["results"] = results
            with self.lock:
                if generation == self.generation and self.capture_allowed and successful:
                    previous = self.state
                    self.gate._accept(successful, completed)
                    if conversational and self.gate.asleep:
                        self.context_reset()
                    if self.gate.dictating and not before["dictating"]:
                        self.last_speech = completed
                    if previous != self.state:
                        self.event("command", previous=previous)
                    if self.dry_run:
                        self.would_run.append(dict(row))
            row["executed"] = not self.dry_run and any(
                result.get("ok") or result.get("started_ms") is not None for result in results)
            row["completed_at"] = completed
            # Only mode transitions chime; no generated speech in v1.
            if any(a["action"] == "mode" for a in successful) and not self.dry_run:
                self.chimes.cue()
        row["state_after"] = self.state
        timings["total_ms"] = (self.clock() - entered) * 1000
        timings["end_to_end_ms"] = max(0.0, (self.clock() - speech_end) * 1000)
        with self.lock:
            self.decision_count += 1
            self.reason_counts[row["reason"]] += 1
        self.log(row)
        return row


def listen(root, loaded, *, backend_name, device="cpu", dry_run=False, minutes=None,
           backend=None, microphone=None, controls=None, segmenter=None, session=None,
           stop=None, output=print, startup_timeout=5.0, frame_timeout=2.0):
    """One process, separate capture, segmentation and recognition/execution workers.

    Queue overflow or native worker failure stops capture and discards work.
    No inference or action holds the capture controller's lock.
    """
    import numpy as np

    from .audio import SpeechSegmenter
    from .ptt import DefaultInput, WindowsHotkey, parse_hotkey
    from .stt import load_backend
    from .stt.base import SAMPLE_RATE, transcribe_segment

    if _UNSAFE.is_set():
        raise RuntimeError("A previous native worker did not close; exit this process before retrying")
    # Validate before acquiring any model or microphone resource.
    gate = configured_gate(root, loaded) if session is None else session.gate
    log = DecisionLog(root)
    stop = stop or threading.Event()

    def execute(plan):
        if stop.is_set():
            revoke_for_shutdown()
            return [{"ok": False, "detail": "Listening stopped; capture authorization revoked"}]
        session.control(locked=not controls.available())
        if stop.is_set():
            revoke_for_shutdown()
            return [{"ok": False, "detail": "Listening stopped; capture authorization revoked"}]
        if not session.capture_allowed:
            return [{"ok": False, "detail": "Session is locked or microphone is Off"}]
        from .executor import Context
        from .executor import execute as run
        with Context(root, loaded=loaded, monitor_reference=session.monitor_reference) as context:
            if stop.is_set():
                revoke_for_shutdown()
                return [{"ok": False, "detail": "Listening stopped; capture authorization revoked"}]
            return run(plan, context=context)

    needs_resolver = session is None
    if needs_resolver:
        def foreground():
            from .executor.app_actions import foreground_app
            return foreground_app(root, loaded)

        session = GateSession(gate, execute=execute, dry_run=dry_run, log=log,
                              foreground_app=foreground)
    audio, segments = queue.Queue(maxsize=64), queue.Queue(maxsize=8)
    failures, causes = [], []
    failure_lock = threading.Lock()
    stop_reason = "external_stop"
    recognition_in_flight = threading.Event()
    valid_frame_seen = threading.Event()
    capture_suspended = False
    finish_allowed = dry_run and session.dry_run and minutes is not None
    producer_done, recognition_drained, capture_sealed = (threading.Event() for _ in range(3))
    measurement_start, measurement_end = [None], [None]
    excluded_callback_samples = [0]
    minimum_end = finish_deadline = None
    finishing_at = sealed_wall_at = drained_wall_at = None
    audio_drops = []
    shutdown_generation = None
    shutdown_discarded_recognition = 0
    shutdown_discarded_audio_frames = 0
    capture_stop, capture_closed = threading.Event(), threading.Event()
    capture_closed.set()
    capture_thread, capture_generation = None, None
    last_frame = [None]
    capture_started = None
    began = time.perf_counter()

    def revoke_for_shutdown():
        nonlocal shutdown_generation
        with session.lock:
            if shutdown_generation is None:
                shutdown_generation = session.generation
            stop.set()
            capture_stop.set()
            session.control(enabled=False)

    def record_failure(error, phase):
        row = {"event": "failure", "phase": phase, "type": type(error).__name__,
               "message": str(error) or type(error).__name__, "at": time.perf_counter(),
               "worker": threading.current_thread().name}
        with failure_lock:
            failures.append(row)
            causes.append(error)
        try:
            session.log(row)
        except BaseException as log_error:  # noqa: BLE001 - logging must not replace the original failure
            with failure_lock:
                failures.append({"event": "failure", "phase": "error_log",
                                 "type": type(log_error).__name__, "message": str(log_error),
                                 "at": time.perf_counter(), "worker": threading.current_thread().name})
                causes.append(log_error)

    def fail(error, phase):
        nonlocal stop_reason
        record_failure(error, phase)
        with failure_lock:
            if stop_reason not in ("worker_failure", "controller_failure", "interrupted"):
                stop_reason = "controller_failure" if phase == "capture_deadline" else "worker_failure"
        stop.set()
        capture_stop.set()
        try:
            session.control(enabled=False)
        except BaseException as log_error:  # noqa: BLE001 - preserve shutdown despite logging failure
            record_failure(log_error, "revoke")

    def capture(generation):
        count, origin = 0, None

        def receive(pcm, frames, status):
            nonlocal count, origin, minimum_end, finish_deadline
            if capture_sealed.is_set() and not stop.is_set():
                if status or not frames or len(pcm) != frames * 2:
                    fail(RuntimeError(f"Invalid microphone frame: {status or 'PCM length'}"), "capture")
                else:
                    # Stream callbacks are ordered: these start after the last
                    # enqueued callback, hence strictly outside its sealed interval.
                    excluded_callback_samples[0] += frames
                return
            if capture_stop.is_set() or stop.is_set():
                return
            if status or not frames or len(pcm) != frames * 2:
                fail(RuntimeError(f"Invalid microphone frame: {status or 'PCM length'}"), "capture")
                return
            last_frame[0] = time.perf_counter()
            if origin is None:
                origin = time.perf_counter() - frames / SAMPLE_RATE
            at = origin + count / SAMPLE_RATE
            if measurement_start[0] is None:
                if finish_allowed:
                    # A successful timed soak measures N minutes of captured
                    # samples. Stream initialization cannot consume that time.
                    minimum_end = at + minutes * 60
                    finish_deadline = minimum_end + 20.0
                measurement_start[0] = at
            valid_frame_seen.set()
            count += frames
            try:
                audio.put_nowait((generation, pcm, at))
            except queue.Full:
                fail(RuntimeError("Audio backlog exceeded its bound; no partial command is safe"), "capture")
        try:
            with microphone.open(receive) as info:
                session.event("stream_opened", stream_open=True, device=info)
                while not capture_stop.wait(.02) and not stop.is_set():
                    pass
            session.event("stream_closed", stream_open=False)
        except BaseException as error:  # noqa: BLE001 - transfer all native capture worker failures
            if microphone.is_open:
                _UNSAFE.set()
            fail(error, "capture")
        finally:
            capture_closed.set()

    def segment_audio():
        nonlocal shutdown_discarded_audio_frames, sealed_wall_at
        previous = None
        try:
            while not stop.is_set():
                try:
                    generation, pcm, at = audio.get(timeout=.05)
                except queue.Empty:
                    if capture_sealed.is_set() and capture_closed.is_set():
                        producer_done.set()
                        return
                    continue
                if generation != session.generation or not session.capture_allowed:
                    with session.lock:
                        if shutdown_generation is not None and generation == shutdown_generation:
                            shutdown_discarded_audio_frames += len(pcm) // 2
                    continue
                if previous != generation:
                    segmenter.reset()
                    if finish_allowed:
                        segmenter.request_seal(minimum_end)
                    previous = generation
                if capture_sealed.is_set() and at < measurement_end[0] - 2 / SAMPLE_RATE:
                    raise RuntimeError("Queued audio overlaps the sealed measurement interval")
                blocks = segmenter.feed(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768, at)
                if segmenter.last_speech_at is not None:
                    session.speech(segmenter.last_speech_at)
                for dropped in segmenter.drain_drops():
                    row = {"event": "audio_drop", "reason": dropped.reason,
                           "start": dropped.start, "end": dropped.end}
                    audio_drops.append(row)
                    log(row)
                for block in blocks:
                    try:
                        segments.put_nowait((generation, block))
                    except queue.Full:
                        raise RuntimeError("Recognition backlog exceeded its bound; capture stopped") from None
                if finish_allowed and segmenter.sealed_at is not None and not capture_sealed.is_set():
                    measurement_end[0] = segmenter.sealed_at
                    sealed_wall_at = time.perf_counter()
                    capture_sealed.set()
                    capture_stop.set()
                    session.event("measurement_sealed", measurement_end=measurement_end[0],
                                  phase="draining")
        except BaseException as error:  # noqa: BLE001 - transfer all audio worker failures
            fail(error, "audio")

    def recognized_decision(row, generation):
        nonlocal shutdown_discarded_recognition
        with session.lock:
            if (shutdown_generation is not None and generation == shutdown_generation
                    and row["reason"] == "stale_or_closed"):
                shutdown_discarded_recognition += 1

    def recognize():
        nonlocal drained_wall_at
        try:
            while not stop.is_set():
                try:
                    generation, block = segments.get(timeout=.05)
                except queue.Empty:
                    if finish_allowed and producer_done.is_set():
                        drained_wall_at = time.perf_counter()
                        recognition_drained.set()
                        return
                    continue
                if stop.is_set():
                    revoke_for_shutdown()
                if generation != session.generation or not session.capture_allowed:
                    row = session.hear("", start=block.start, end=block.end, speech_end=block.speech_end,
                                       generation=generation)
                    recognized_decision(row, generation)
                    continue
                if session.chimes.overlaps(block.start, block.end):
                    row = session.hear("", start=block.start, end=block.end, speech_end=block.speech_end,
                                       generation=generation)
                    recognized_decision(row, generation)
                    continue
                events = []
                entered = time.perf_counter()
                recognition_in_flight.set()
                try:
                    transcribe_segment(backend, block.audio, events.append)
                    finals = {event.line_id: event for event in events if event.final}
                    text = " ".join(event.text.strip() for event in sorted(
                        finals.values(), key=lambda e: (e.start_sample, e.line_id)) if event.text.strip())
                    if stop.is_set():
                        revoke_for_shutdown()
                    row = session.hear(text, start=block.start, end=block.end, speech_end=block.speech_end,
                                       generation=generation, timings={
                                           "stt_ms": (time.perf_counter() - entered) * 1000,
                                           "queue_ms": max(0, (entered - block.end) * 1000),
                                           "endpoint_ms": (block.end - block.speech_end) * 1000})
                    recognized_decision(row, generation)
                    output(json.dumps(row, ensure_ascii=False))
                finally:
                    recognition_in_flight.clear()
        except BaseException as error:  # noqa: BLE001 - transfer all recognition worker failures
            fail(error, "recognition")

    workers = [threading.Thread(target=segment_audio, name="voicebar-audio", daemon=True),
               threading.Thread(target=recognize, name="voicebar-recognition", daemon=True)]
    started_workers = []
    backend_close_attempted = backend_closed = False
    try:
        controls = controls or WindowsHotkey(parse_hotkey("ctrl+alt+f8"))
        microphone = microphone or DefaultInput()
        segmenter = segmenter or SpeechSegmenter(root=root)
        backend = backend or load_backend(backend_name, root, loaded.catalog, device=device)
        if needs_resolver:
            from .resolver import configured_resolver
            session.resolver = configured_resolver(Path(root))
        began = time.perf_counter()  # listening duration excludes local model initialization
        if minutes is not None:
            minimum_end = began + minutes * 60
            # This bounds normal endpoint completion and recognition together;
            # uncertain native cleanup retains its separate unchanged joins.
            finish_deadline = minimum_end + 20.0
        session.event("started", stream_open=False, backend=backend_name, device=device,
                      dry_run=dry_run, log=str(log.path))
        output(f"Listening in {session.gate.mode} mode; {'dry-run' if dry_run else 'execution enabled'}. Log: {log.path}")
        for worker in workers:
            worker.start()
            started_workers.append(worker)
        while not stop.is_set():
            session.control(locked=not controls.available())
            session.tick()
            with session.lock:
                if shutdown_generation is None and not session.capture_allowed:
                    capture_suspended = True
            now = time.perf_counter()
            if minutes is not None and now >= minimum_end:
                if not finish_allowed:
                    stop_reason = "duration"
                    break
                if measurement_start[0] is None:
                    # The initial began + N deadline remains a no-audio exit,
                    # whose missing valid frames always make it incomplete.
                    stop_reason = "duration"
                    break
                if finishing_at is None:
                    finishing_at = now
                    session.event("finishing", phase="endpoint", minimum_end=minimum_end,
                                  finish_deadline=finish_deadline)
                if stop.is_set():
                    break
                if not session.capture_allowed:
                    capture_suspended = True
                    stop_reason = "capture_suspended"
                    break
                if (producer_done.is_set() and recognition_drained.is_set() and capture_closed.is_set()
                        and drained_wall_at <= finish_deadline):
                    stop_reason = "duration"
                    break
                if now >= finish_deadline:
                    stop_reason = "finish_timeout"
                    phase = "endpoint" if measurement_end[0] is None else "recognition"
                    record_failure(RuntimeError(f"Timed soak finishing expired during {phase}; coverage incomplete"),
                                   "finish_timeout")
                    break
            if capture_thread is not None:
                first = last_frame[0]
                if not capture_sealed.is_set() and ((first is None and now - capture_started >= startup_timeout)
                        or (first is not None and now - first >= frame_timeout)):
                    fail(RuntimeError("Microphone frame deadline expired; capture authorization revoked"),
                         "capture_deadline")
                    break
            allowed = session.capture_allowed
            if capture_thread is not None and (not allowed or capture_closed.is_set()
                                               or capture_generation != session.generation):
                capture_stop.set()
                capture_thread.join(3)
                if capture_thread.is_alive():
                    _UNSAFE.set()
                    raise RuntimeError("Microphone closure unconfirmed; exit the process before retrying")
                capture_thread = None
            if allowed and capture_thread is None and not capture_sealed.is_set():
                capture_stop.clear()
                capture_closed.clear()
                capture_generation = session.generation
                capture_started = time.perf_counter()
                last_frame[0] = None
                capture_thread = threading.Thread(target=capture, args=(session.generation,),
                                                  name="voicebar-capture", daemon=True)
                capture_thread.start()
            stop.wait(.05)
    except KeyboardInterrupt as error:
        stop_reason = "interrupted"
        record_failure(error, "interrupt")
    except BaseException as error:  # noqa: BLE001 - retain controller failures through cleanup
        stop_reason = "controller_failure"
        record_failure(error, "controller")
    finally:
        try:
            revoke_for_shutdown()
        except BaseException as error:  # noqa: BLE001 - finish native cleanup despite logging failure
            record_failure(error, "shutdown_control")
        if capture_thread is not None:
            try:
                capture_thread.join(3)
            except BaseException as error:  # noqa: BLE001 - collect every uncertain cleanup
                _UNSAFE.set()
                record_failure(error, "capture_join")
        for worker in started_workers:
            try:
                worker.join(3)
            except BaseException as error:  # noqa: BLE001 - retain the primary failure
                _UNSAFE.set()
                record_failure(error, "worker_join")
        alive = [t.name for t in [*workers, capture_thread] if t is not None and t.is_alive()]
        # Never close a native model while its inference worker still owns it.
        if backend is not None and not alive:
            backend_close_attempted = True
            try:
                backend.close()
                backend_closed = True
            except BaseException as error:  # noqa: BLE001 - uncertain native cleanup requires process exit
                _UNSAFE.set()
                record_failure(error, "backend_close")
        try:
            stream_open = bool(microphone.is_open) if microphone is not None else False
        except BaseException as error:  # noqa: BLE001 - unknown stream state is not a confirmed close
            stream_open = None
            _UNSAFE.set()
            record_failure(error, "stream_status")
        if alive:
            _UNSAFE.set()
            record_failure(RuntimeError("Native worker cleanup unconfirmed; exit process: " + ", ".join(alive)),
                           "worker_cleanup")
        if stream_open:
            _UNSAFE.set()
            record_failure(RuntimeError("Microphone stream closure unconfirmed; exit process before retrying"),
                           "stream_cleanup")
        try:
            session.event("stopped", stream_open=stream_open, workers_alive=alive)
        except BaseException as error:  # noqa: BLE001 - retain results even when final logging fails
            record_failure(error, "shutdown_log")
        with audio.mutex:
            audio_frames = sum(len(item[1]) // 2 for item in audio.queue)
            audio_chunks = len(audio.queue)
        with session.lock, failure_lock:
            session.report_would_run = deepcopy(session.would_run)
            pending_segments = segments.qsize()
            in_flight = recognition_in_flight.is_set()
            segmenter_frames = len(getattr(segmenter, "_pending", ()))
            active_speech = getattr(segmenter, "_active_start", None) is not None
            dropping = bool(getattr(segmenter, "_dropping", False))
            coverage_complete = not (
                audio_frames or pending_segments or in_flight or segmenter_frames or active_speech or dropping
                or shutdown_discarded_recognition or shutdown_discarded_audio_frames)
            coverage_complete = coverage_complete and not audio_drops
            if minutes is not None:
                coverage_complete = coverage_complete and valid_frame_seen.is_set() and not capture_suspended
            if finish_allowed:
                coverage_complete = (coverage_complete and measurement_end[0] is not None
                                     and producer_done.is_set() and recognition_drained.is_set()
                                     and measurement_end[0] >= measurement_start[0] + minutes * 60)
            complete = (stop_reason == "duration" and not failures and not alive
                        and stream_open is False and coverage_complete)
            session.outcome = {
                "status": "complete" if complete else (
                    "interrupted" if stop_reason == "interrupted" else "failed" if failures else "stopped"),
                "complete": complete, "coverage_complete": coverage_complete,
                "valid_frame_seen": valid_frame_seen.is_set(), "capture_suspended": capture_suspended,
                "measurement_start": measurement_start[0], "measurement_end": measurement_end[0],
                "measurement_seconds": (None if measurement_end[0] is None or measurement_start[0] is None
                                        else measurement_end[0] - measurement_start[0]),
                "capture_start_offset_seconds": (None if measurement_start[0] is None
                                                 else measurement_start[0] - began),
                "finishing": {"allowed": finish_allowed, "started_at": finishing_at,
                              "minimum_end": minimum_end, "deadline": finish_deadline,
                              "sealed_at": sealed_wall_at, "producer_done": producer_done.is_set(),
                              "recognition_drained": recognition_drained.is_set(),
                              "drained_at": drained_wall_at},
                "audio_drops": deepcopy(audio_drops),
                "excluded_samples": (getattr(segmenter, "excluded_samples", 0)
                                     + excluded_callback_samples[0]),
                "elapsed_seconds": max(0.0, time.perf_counter() - began),
                "stop_reason": stop_reason, "failures": deepcopy(failures),
                "shutdown_discarded_recognition": shutdown_discarded_recognition,
                "shutdown_discarded_audio_frames": shutdown_discarded_audio_frames,
                "would_run": session.report_would_run,
                "decision_count": session.decision_count, "reason_counts": dict(session.reason_counts),
                "cleanup": {"stream_open": stream_open, "workers_alive": list(alive),
                            "backend_close_attempted": backend_close_attempted,
                            "backend_closed": backend_closed, "unsafe": _UNSAFE.is_set()},
                "pending": {"audio_frames": audio_frames, "audio_chunks": audio_chunks,
                            "segments": pending_segments, "recognition_in_flight": in_flight,
                            "segmenter_frames": segmenter_frames, "active_speech": active_speech,
                            "dropping": dropping},
            }
    if causes:
        raise ListeningFailure(session, log.path, session.outcome, causes[0]) from causes[0]
    return session, log.path
