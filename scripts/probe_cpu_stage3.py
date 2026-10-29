"""One saved-WAV CPU deadline probe; never a 40-command accuracy benchmark.

Run with the isolated STT Python, --backend qwen or whisper-large and --output.
The worker is identified/contained before loading and released only after READY.
No microphone, hotkeys, app windows, input injection or executor is imported.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

PREFIX = "VOICEBAR_CPU_PROBE "
DEFAULT_SESSION = "20261010T165301Z-check-0ffa4a3f"
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
               "TRANSFORMERS_OFFLINE": "1", "DO_NOT_TRACK": "1"}


def emit(record):
    print(PREFIX + json.dumps(record, ensure_ascii=True), flush=True)


def selected_item(root, session):
    from voicebar.catalog import load_catalog
    from voicebar.stt.benchmark import personal_items

    if not session or Path(session).name != session or session in (".", ".."):
        raise ValueError("Session must be an exact directory name under fixtures/mine.")
    loaded = load_catalog(root)
    items, _ = personal_items(root, loaded)
    selected = [item for item in items if item["id"] == session + "/p001"]
    if len(selected) != 1:
        raise ValueError("The requested complete validated session has no unique p001 WAV.")
    return loaded, selected[0]


def worker(args):
    os.environ.update(OFFLINE_ENV)
    sys.path.insert(0, str(args.root.resolve()))
    # This handshake occurs before project/model imports or model descendants.
    emit({"kind": "hello", "pid": os.getpid()})
    if sys.stdin.readline().strip() != "LOAD":
        raise RuntimeError("Worker was not released into its owned cleanup job.")
    from voicebar.parser import parse
    from voicebar.schema import validate_plan
    from voicebar.stt import load_backend
    from voicebar.stt.base import SAMPLE_RATE, read_wav, transcribe_wav
    from voicebar.stt.offline import SocketAuditPolicy

    policy = SocketAuditPolicy(emit)
    canaries = policy.install_and_check()
    began = time.perf_counter()
    loaded, item = selected_item(args.root, args.session)
    audio_seconds = len(read_wav(Path(item["wav"]))) / SAMPLE_RATE
    catalog_ms = (time.perf_counter() - began) * 1000
    began = time.perf_counter()
    backend = load_backend(args.backend, args.root, loaded.catalog, device="cpu")
    load_ms = (time.perf_counter() - began) * 1000
    details = dict(getattr(backend, "runtime_details", getattr(backend, "placement", {})))
    emit({"kind": "ready", "pid": os.getpid(), "backend": args.backend,
          "device": "cpu", "catalog_validation_ms": catalog_ms,
          "cold_load_ms": load_ms, "runtime_details": details,
          "audit_canaries": canaries, "id": item["id"],
          "audio_seconds": audio_seconds,
          "wav": item["wav"], "wav_sha256": item["wav_sha256"],
          "expected_plan": item["expected_plan"]})
    try:
        if sys.stdin.readline().strip() != "GO":
            raise RuntimeError("Worker inference was not released by the parent.")
        started = time.perf_counter()
        result = transcribe_wav(backend, Path(item["wav"]), realtime=True)
        plan = parse(result["text"], loaded.catalog, last_app=item.get("last_app"))
        if plan is not None:
            validate_plan(plan)
        attempts = [record for record in policy.events if record.get("phase") == "runtime"]
        emit({"kind": "result", "id": item["id"], "wav_sha256": item["wav_sha256"],
              "inference_elapsed_ms": (time.perf_counter() - started) * 1000,
              "expected_plan": item["expected_plan"], "plan": plan,
              "exact_plan": plan == item["expected_plan"],
              "audit_runtime_attempts": attempts, "recognition": result,
              "runtime_details": dict(getattr(backend, "runtime_details", getattr(backend, "placement", {})))})
        return 0 if not attempts else 1
    finally:
        backend.close()


def reader(stream, channel, messages):
    try:
        for line in stream:
            messages.put((channel, line))
    finally:
        messages.put((channel, None))


def verify_worker_pid(pid, launcher_pid, monitor):
    from voicebar.stt.offline import descendant_pids

    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise RuntimeError("Worker sent an invalid real PID.")
    processes = monitor.processes()
    present = {value for value, _ in processes}
    owned = descendant_pids(processes, {launcher_pid})
    if pid not in present or pid not in owned:
        raise RuntimeError("Real worker PID is not a current descendant of the owned launcher.")
    return sorted(owned & present)


def run_probe(args):
    sys.path.insert(0, str(args.root.resolve()))
    from voicebar.stt.offline import NativeEndpointMonitor, WorkerJob

    if os.name != "nt":
        raise RuntimeError("This probe requires native Windows process containment.")
    environment = {**os.environ, **OFFLINE_ENV, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(args.root.resolve())}
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child",
               "--root", str(args.root.resolve()), "--backend", args.backend,
               "--session", args.session]
    monitor = NativeEndpointMonitor()
    began = time.perf_counter()
    process = subprocess.Popen(command, cwd=args.root, env=environment, shell=False,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    messages = queue.Queue()
    readers = [threading.Thread(target=reader, args=(stream, channel, messages), daemon=True)
               for channel, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for thread in readers:
        thread.start()
    report = {
        "version": 1, "kind": "single-WAV CPU deadline probe", "backend": args.backend,
        "device": "cpu", "session": args.session, "prompt_id": "p001",
        "command": command, "launcher_pid": process.pid, "worker_pid": None,
        "offline_environment": OFFLINE_ENV, "worker_job_contained": False,
        "initialization_timeout_seconds": args.init_timeout,
        "recognition_timeout_seconds_after_audio_arrival": args.inference_timeout,
        "inference_deadline_seconds": None,
        "inference_deadline_origin": "parent GO write plus validated WAV duration plus recognition timeout",
        "records": [], "stdout_log": [], "stderr": [], "cleanup_errors": [],
        "completed": False, "timed_out": False, "timeout_phase": None,
        "probe_denominator": 1, "exact_plans": 0,
        "accuracy_over_40": None, "p50_eos_ms": None, "p95_eos_ms": None,
        "limitations": [
            "One prerecorded p001 WAV is a deadline smoke, not a 40-command accuracy estimate.",
            "The deadline allows the validated WAV duration plus 30 seconds after the paced audio arrival clock.",
            "This approximates PTT's release allowance; real microphone closure, capture queues and loss are not simulated.",
            "No EOS quantile or full offline endpoint proof is established by this probe.",
            "Offline environment and Python socket audit refuse library network entry points; native endpoints are not sampled.",
        ],
    }
    job, worker_pid, phase = None, None, "initialization"
    init_deadline, inference_began, inference_deadline = began + args.init_timeout, None, None
    error = None

    def accept(channel, line):
        nonlocal job, worker_pid, phase, inference_began, inference_deadline
        if line is None:
            return
        if channel != "stdout" or not line.startswith(PREFIX):
            report["stderr" if channel == "stderr" else "stdout_log"].append(line.rstrip("\r\n"))
            return
        record = json.loads(line[len(PREFIX):])
        if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
            raise TypeError("Worker sent an invalid protocol record.")
        report["records"].append(record)
        kind = record["kind"]
        if kind == "hello":
            if worker_pid is not None:
                raise RuntimeError("Worker sent duplicate identity.")
            worker_pid = record.get("pid")
            report["observed_owned_pids"] = verify_worker_pid(worker_pid, process.pid, monitor)
            job = WorkerJob(worker_pid)
            report["worker_pid"], report["worker_job_contained"] = worker_pid, True
            process.stdin.write("LOAD\n")
            process.stdin.flush()
        elif kind == "ready":
            if (job is None or phase != "initialization" or record.get("pid") != worker_pid
                    or record.get("audit_canaries") != 4 or record.get("device") != "cpu"
                    or record.get("backend") != args.backend):
                raise RuntimeError("Loaded worker readiness was not verified.")
            verify_worker_pid(worker_pid, process.pid, monitor)
            duration = record.get("audio_seconds")
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                    or not math.isfinite(duration) or not 0 < duration <= 120):
                raise RuntimeError("Worker WAV duration was not valid for the release deadline.")
            report["ready"] = record
            inference_began = time.perf_counter()
            inference_deadline = inference_began + duration + args.inference_timeout
            report["inference_deadline_seconds"] = duration + args.inference_timeout
            phase = "inference"
            process.stdin.write("GO\n")
            process.stdin.flush()
            process.stdin.close()
        elif kind == "result":
            if phase != "inference" or "result" in report:
                raise RuntimeError("Unexpected or duplicate inference result.")
            report["result_received_after_go_ms"] = (time.perf_counter() - inference_began) * 1000
            if time.perf_counter() > inference_deadline:
                raise TimeoutError("Inference result arrived after its deadline.")
            report["result"] = record
            report["completed"] = True
            report["exact_plans"] = int(record.get("exact_plan") is True)
            phase = "finished"
        elif kind == "fatal":
            raise RuntimeError(record.get("error", "Worker failed."))
        elif kind == "socket_refused" and record.get("phase") == "runtime":
            raise RuntimeError("Worker attempted a runtime network operation.")

    try:
        while phase != "finished":
            deadline = init_deadline if phase == "initialization" else inference_deadline
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"{phase} exceeded its configured deadline.")
            try:
                accept(*messages.get(timeout=min(0.05, remaining)))
            except queue.Empty:
                if process.poll() is not None and messages.empty():
                    raise RuntimeError("Worker exited before returning a result.")
        # Result already recorded within the deadline; bound graceful exit too.
        process.wait(timeout=5)
        if process.returncode != 0:
            raise RuntimeError(f"Worker exited with code {process.returncode}.")
    except TimeoutError as failure:
        report["timed_out"], report["timeout_phase"] = True, phase
        error = str(failure)
    except (RuntimeError, OSError, ValueError, TypeError, subprocess.TimeoutExpired) as failure:
        error = str(failure)
    finally:
        # Closing stdin releases a pre-LOAD worker with EOF. No model can have
        # loaded before its real PID was verified and assigned to this job.
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if job is not None:
            try:
                job.close()  # kills only this verified worker and its descendants
            except (RuntimeError, OSError) as failure:
                report["cleanup_errors"].append(str(failure))
        if process.poll() is None:
            process.terminate()  # only the owned Popen launcher, never app processes
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=1)
        while not messages.empty():
            channel, line = messages.get_nowait()
            if line is not None:
                # Cleanup output cannot authorize another LOAD/GO or result.
                report["stderr" if channel == "stderr" else "stdout_log"].append(line.rstrip("\r\n"))
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    report.update({"returncode": process.returncode, "error": error,
                   "elapsed_seconds": time.perf_counter() - began,
                   "ok": bool(error is None and report["completed"] and not report["cleanup_errors"]),
                   "inference_elapsed_before_cleanup_ms": (
                       (time.perf_counter() - inference_began) * 1000 if inference_began is not None else None)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "backend", "device", "ok", "completed", "timed_out", "timeout_phase", "exact_plans", "probe_denominator", "error"
    )}, indent=2), flush=True)
    return 0 if report["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=Path("C:/dev/voicebar"))
    parser.add_argument("--backend", choices=("qwen", "whisper-large", "moonshine-medium"), required=True)
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--init-timeout", type=float, default=180.0)
    parser.add_argument("--inference-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    if not 0 < args.init_timeout <= 180 or not 0 < args.inference_timeout <= 30:
        parser.error("Initialization must be <=180 seconds and recognition after arrival <=30 seconds.")
    if args.child:
        try:
            return worker(args)
        except (RuntimeError, OSError, ValueError, TypeError, ImportError) as failure:
            emit({"kind": "fatal", "error": str(failure)})
            return 1
    if args.output is None:
        parser.error("--output is required for the parent deadline probe.")
    return run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
