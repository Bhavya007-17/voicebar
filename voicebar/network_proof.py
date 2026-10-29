"""Disposable Stage 8 app replay and psutil process-tree socket evidence.

This observes and guards the child it creates. It changes no network adapter,
firewall, startup shortcut, user application or machine-wide setting. Sampling
cannot prove the absence of endpoints born and gone between samples; Python
audit refusal adds coverage but native extensions can bypass those hooks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .stt.offline import (
    OFFLINE_ENV,
    OfflineViolation,
    SocketAuditPolicy,
    WorkerJob,
    _reader,
    read_manifest,
)

PREFIX = "VOICEBAR_STAGE8 "


def emit(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


class LocalSocketPolicy(SocketAuditPolicy):
    """Reject listeners, UDP, DNS and every destination except exact IPv4 loopback."""

    def __call__(self, event, arguments):
        if event == "socket.connect":
            address = arguments[1]
            if (isinstance(address, tuple) and len(address) == 2
                    and address[0] == "127.0.0.1"):
                return
        if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
            if arguments[0] in ("127.0.0.1", b"127.0.0.1"):
                return
            # Base policy allows ::1 resolution; Stage 8's exception is exact.
            record = {"event": event, "phase": self.phase, "monotonic": time.perf_counter()}
            self.events.append(record)
            if self.emit is not None:
                self.emit({"kind": "socket_refused", **record})
            raise OfflineViolation(f"Local proof refused {event} before its system call")
        super().__call__(event, arguments)


def endpoint_reason(endpoint):
    """Take plain dictionaries so monitor policy is independently testable."""
    if endpoint["status"] == "LISTEN":
        return "listening socket"
    if endpoint["type"] != int(socket.SOCK_STREAM):
        return "UDP or unsupported endpoint"
    if endpoint["status"] == "CLOSE":
        return None
    remote = endpoint["remote"]
    if not remote or remote[0] != "127.0.0.1":
        return "connection outside exact 127.0.0.1"
    return None


class PsutilMonitor:
    """Retain Process identities, including descendants whose parent later exits."""

    def __init__(self, pid):
        import psutil
        self.psutil = psutil
        process = psutil.Process(pid)
        self.processes = {(process.pid, process.create_time()): process}

    def sample(self):
        psutil = self.psutil
        covered = set()
        for identity, process in list(self.processes.items()):
            if identity in covered:
                continue
            try:
                for child in process.children(recursive=True):
                    child_identity = (child.pid, child.create_time())
                    self.processes[child_identity] = child
                    covered.add(child_identity)
                covered.add(identity)
            except psutil.NoSuchProcess:
                continue
            # AccessDenied is deliberately fatal: an unreadable process is not
            # evidence of zero network activity.
        endpoints = []
        pids = {identity[0] for identity in self.processes}
        # Windows' per-process API rereads the same system endpoint tables.
        # Read those tables once and retain only the owned process tree.
        for connection in psutil.net_connections(kind="inet"):
            if connection.pid in pids:
                endpoints.append({"pid": connection.pid, "family": int(connection.family),
                                  "type": int(connection.type), "status": str(connection.status),
                                  "local": list(connection.laddr), "remote": list(connection.raddr)})
        return sorted(pids), endpoints


def physical_adapters():
    """Read Windows' physical-adapter state; never disable or enable anything."""
    if os.name != "nt":
        raise OfflineViolation("Physical offline verification requires Windows")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "@(Get-NetAdapter -Physical -ErrorAction Stop | Select-Object Name,Status) | ConvertTo-Json -Compress"]
    completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    value = json.loads(completed.stdout)
    rows = [value] if isinstance(value, dict) else value
    if not isinstance(rows, list) or not rows:
        raise OfflineViolation("No physical adapters were returned; offline state is unproven")
    return rows


def _offline(adapters):
    return bool(adapters) and all(row.get("Status") in {"Disabled", "Disconnected", "Not Present"}
                                  for row in adapters)


class WavInput:
    """A test-owned microphone boundary; WAV frames take the normal capture path."""

    def __init__(self, opened):
        self.opened, self.is_open, self.callback = opened, False, None

    @contextmanager
    def open(self, callback):
        self.callback, self.is_open = callback, True
        self.opened.put(self)
        try:
            yield {"name": "Stage 8 owned WAV replay", "index": None}
        finally:
            self.is_open = False


def worker(root, manifest, backend_name, device, *, gui=True, fixture=False):
    os.environ.update(OFFLINE_ENV)
    policy = LocalSocketPolicy(emit=emit)
    canaries = policy.install_and_check()
    emit({"kind": "ready", "pid": os.getpid(), "audit_canaries": canaries})
    if sys.stdin.readline().strip() != "GO":
        raise OfflineViolation("Parent did not release monitored app startup")

    import numpy as np

    from .catalog import load_catalog
    from .stt.base import SAMPLE_RATE, TextEvent, read_wav
    from .ui.controller import RuntimeController

    items = read_manifest(manifest)
    loaded = load_catalog(root)
    decisions, opened = queue.Queue(), queue.Queue()
    planned = []

    def record_execution(plan, cancel):
        if cancel.is_set():
            raise RuntimeError("Replay execution was cancelled")
        planned.extend(plan)
        return [{"ok": True, "detail": "Stage 8 dry-run: no desktop action"} for _ in plan]

    class FixtureRecognizer:
        """Only pytest selects this deterministic recognizer; real proof never does."""
        text = ""

        def transcribe_segment(self, audio, output):
            output(TextEvent("fixture", self.text, True, 0, len(audio), time.perf_counter(), "fixture"))

        def close(self):
            pass

    fixture_backend = FixtureRecognizer() if fixture else None

    class HeldOnlySegmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, *args):
            raise AssertionError("Fixture replay must use the held microphone path")

        def drain_drops(self):
            return []

    controller = RuntimeController(
        root, loaded, backend_name=backend_name, device=device,
        backend_factory=(lambda *args, **kwargs: fixture_backend) if fixture else None,
        segmenter_factory=HeldOnlySegmenter if fixture else None,
        microphone_factory=lambda index: WavInput(opened),
        executor=record_execution, available=lambda: True, devices=list,
        foreground_app=lambda: None)
    # Suppress only the speaker cue: the proof does not play sounds into a real
    # microphone. The injected WAV input has its own isolated sample stream.
    controller.session.chimes.play = lambda: None
    original_log = controller.session.log

    def log(row):
        original_log(row)
        if row.get("event") == "decision":
            decisions.put(row)

    controller.session.log = log
    application = desktop = None
    if gui:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        class OwnedHotkeys:
            """Proof does not seize the user's global keys or Copilot key."""
            error = None
            copilot_error = None

            def __init__(self, *args, **kwargs):
                self.stopped = threading.Event()

            def start(self):
                self.stopped.clear()

            def close(self):
                self.stopped.set()

        from .ui import app as desktop_module

        # Also cover a hotkey-setting reload inside this disposable process.
        desktop_module.HotkeyManager = OwnedHotkeys
        application = QApplication([])
        application.setQuitOnLastWindowClosed(False)
        desktop = desktop_module.DesktopApp(application, root, loaded, controller=controller)
        desktop.start()
    else:
        controller.start()

    outcome = {"ok": False, "items": len(items), "results": 0}

    def replay():
        try:
            if not controller.ready.wait(180):
                raise OfflineViolation("App did not load its local model within 180 seconds")
            emit({"kind": "loaded", "backend": backend_name, "device": device,
                  "gui": gui, "fixture_recognizer": fixture, "log": str(controller._log.path)})
            for item in items:
                audio = read_wav(Path(item["wav"]))
                if not len(audio) or len(audio) >= 20 * SAMPLE_RATE:
                    raise OfflineViolation(f"{item['id']}: WAV must contain less than 20 seconds")
                if fixture:
                    fixture_backend.text = item["text"]
                controller.hold_start()
                microphone = opened.get(timeout=10)
                origin = time.perf_counter()
                for offset in range(0, len(audio), 512):
                    frame = audio[offset:offset + 512]
                    remaining = origin + (offset + len(frame)) / SAMPLE_RATE - time.perf_counter()
                    if remaining > 0:
                        # PortAudio callbacks deliver an already-captured frame;
                        # its first callback must follow the first frame period.
                        threading.Event().wait(remaining)
                    if not microphone.is_open:
                        raise OfflineViolation("Replay input closed before the WAV completed")
                    pcm = np.clip(np.rint(frame * 32768), -32768, 32767).astype("<i2").tobytes()
                    microphone.callback(pcm, len(frame), "")
                controller.hold_end()
                row = decisions.get(timeout=90)
                emit({"kind": "result", "id": item["id"], "wav": item["wav"],
                      "decision": row, "expected_plan": item.get("expected_plan")})
                outcome["results"] += 1
                # The next hold must wait for its explicit controller completion
                # event, otherwise it could belong to the previous generation.
                deadline = time.perf_counter() + 10
                while controller._holding or controller._hold_pending or controller._stream_open:
                    if time.perf_counter() > deadline:
                        raise OfflineViolation("Held replay did not confirm stream closure")
                    controller.closed.wait(.005)
            outcome["ok"] = True
        except BaseException as error:  # noqa: BLE001 - preserve worker proof failure
            outcome["error"] = f"{type(error).__name__}: {error}"
        finally:
            controller.close()

    if gui:
        replay_thread = threading.Thread(target=replay, daemon=True, name="stage8-wav-replay")
        replay_thread.start()
        timer = QTimer(application)
        timer.setInterval(25)
        timer.timeout.connect(lambda: desktop.quit() if controller.closed.is_set() else None)
        timer.start()
        application.exec()
        replay_thread.join(5)
    else:
        replay()
    if not controller.closed.wait(10):
        outcome.update(ok=False, error="App runtime did not close")
    attempts = [event for event in policy.events if event["phase"] == "runtime"]
    outcome.update(kind="summary", gui=gui, fixture_recognizer=fixture,
                   planned_actions=planned, stream_open=controller._stream_open,
                   runtime_socket_attempts=attempts)
    outcome["ok"] = bool(outcome["ok"] and not controller._stream_open and not attempts)
    emit(outcome)
    return 0 if outcome["ok"] else 1


def prove(root, manifest, *, backend="qwen", device="cuda", interval_ms=10,
          timeout=900, expect_offline=False, gui=True, fixture=False):
    """Start the app child, monitor startup/replay/shutdown, retain every finding."""
    if not 0 < interval_ms <= 10 or timeout <= 0:
        raise ValueError("Proof needs a positive timeout and a sampling interval at most 10 ms")
    root, manifest = Path(root).resolve(), Path(manifest).resolve()
    items = read_manifest(manifest)
    adapters_before = physical_adapters() if expect_offline else None
    if expect_offline and not _offline(adapters_before):
        raise OfflineViolation("Physical networking is still enabled; no adapter was changed")
    command = [sys.executable, "-m", "voicebar.network_proof", "worker", "--root", str(root),
               "--manifest", str(manifest), "--backend", backend, "--device", device]
    if not gui:
        command.append("--headless")
    if fixture:
        command.append("--fixture-recognizer")
    began = time.perf_counter()
    process = subprocess.Popen(command, cwd=root, shell=False, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace",
                               env={**os.environ, **OFFLINE_ENV, "PYTHONUNBUFFERED": "1"})
    report = {"command": command, "backend": backend, "device": device,
              "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
              "adapters_before": adapters_before, "records": [], "samples": [], "violations": [],
              "stderr": [], "stdout": [], "requested_interval_ms": interval_ms,
              "limitations": [
                  "psutil polling can miss endpoints or descendant processes born and gone between samples.",
                  "Python audit refusal cannot guard socket calls made directly by native extensions.",
                  "WAV input replaces physical microphone; executor records plans without desktop side effects.",
                  "Proof GUI owns no global hotkeys; existing applications and network adapters are untouched.",
                  "Existing independent Ollama/Fusion servers are outside the monitored app process tree.",
                  "Observed PIDs are retained; rapid PID reuse can conservatively attribute an unrelated endpoint and fail the proof."]}
    messages = queue.Queue()
    readers = [threading.Thread(target=_reader, args=(stream, channel, messages), daemon=True)
               for channel, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for reader in readers:
        reader.start()
    monitor = None
    released, worker_pid, job, failure, last_sample, max_gap = False, None, None, None, None, 0.0

    def drain():
        nonlocal worker_pid
        while True:
            try:
                channel, line = messages.get_nowait()
            except queue.Empty:
                return
            if line is None:
                continue
            if channel == "stdout" and line.startswith(PREFIX):
                value = json.loads(line[len(PREFIX):])
                report["records"].append(value)
                if value.get("kind") == "ready":
                    if worker_pid is not None or value.get("audit_canaries") != 4:
                        raise OfflineViolation("Invalid app monitoring handshake")
                    worker_pid = value["pid"]
            else:
                report[channel].append(line.rstrip())

    try:
        monitor = PsutilMonitor(process.pid)
        while True:
            sampled = time.perf_counter()
            if last_sample is not None:
                max_gap = max(max_gap, sampled - last_sample)
            last_sample = sampled
            pids, endpoints = monitor.sample()
            if expect_offline:
                stats = monitor.psutil.net_if_stats()
                if any(row["Name"] not in stats or stats[row["Name"]].isup for row in adapters_before):
                    raise OfflineViolation("A physical adapter became active or could not be verified during replay")
            report["samples"].append({"elapsed_ms": (sampled - began) * 1000,
                                      "pids": pids, "endpoints": endpoints})
            for endpoint in endpoints:
                reason = endpoint_reason(endpoint)
                if reason:
                    report["violations"].append({**endpoint, "reason": reason})
            drain()
            if report["violations"]:
                raise OfflineViolation("App or a descendant opened a forbidden endpoint")
            if worker_pid is not None and not released and worker_pid in pids:
                report["worker_image"] = monitor.psutil.Process(worker_pid).exe()
                if os.name == "nt":
                    job = WorkerJob(worker_pid)
                process.stdin.write("GO\n")
                process.stdin.flush()
                process.stdin.close()
                released = True
            if process.poll() is not None:
                break
            if time.perf_counter() - began > timeout:
                raise OfflineViolation(f"App socket proof exceeded {timeout:g} seconds")
            remaining = interval_ms / 1000 - (time.perf_counter() - sampled)
            if remaining > 0:
                threading.Event().wait(remaining)
    except Exception as error:  # noqa: BLE001 - monitoring failure must fail the proof
        failure = f"{type(error).__name__}: {error}"
    finally:
        if job is not None:
            try:
                job.close()
            except Exception as error:  # noqa: BLE001 - retain cleanup failure
                failure = failure or f"Cleanup failed: {error}"
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        for reader in readers:
            reader.join(5)
        try:
            drain()
        except Exception as error:  # noqa: BLE001 - malformed proof output fails closed
            failure = failure or f"Output failed: {error}"
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    adapters_after = physical_adapters() if expect_offline else None
    summaries = [row for row in report["records"] if row.get("kind") == "summary"]
    results = [row for row in report["records"] if row.get("kind") == "result"]
    valid = (len(summaries) == 1 and summaries[0].get("ok") is True
             and summaries[0].get("items") == len(items)
             and summaries[0].get("results") == len(items)
             and len(results) == len(items)
             and {row["id"] for row in results} == {row["id"] for row in items})
    report.update(returncode=process.returncode, monitoring_error=failure,
                  ready_before_load=released, worker_pid=worker_pid,
                  elapsed_seconds=time.perf_counter() - began,
                  sample_count=len(report["samples"]), max_sample_gap_ms=max_gap * 1000,
                  observed_pids=sorted({pid for row in report["samples"] for pid in row["pids"]}),
                  adapters_after=adapters_after,
                  physical_network_disabled=bool(expect_offline and _offline(adapters_after)),
                  ok=bool(valid and released and not failure and process.returncode == 0
                          and not report["violations"]
                          and (not expect_offline or _offline(adapters_after))))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--fixture-recognizer", action="store_true")
    args = parser.parse_args(argv)
    try:
        return worker(args.root, args.manifest, args.backend, args.device,
                      gui=not args.headless, fixture=args.fixture_recognizer)
    except BaseException as error:  # noqa: BLE001 - retain startup failures in evidence
        emit({"kind": "fatal", "error": f"{type(error).__name__}: {error}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
