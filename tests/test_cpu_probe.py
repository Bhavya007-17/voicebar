"""Pure fakes for probe PID ownership and deadline cleanup; no subprocesses."""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("cpu_candidate_probe", Path(__file__).parents[1] / "scripts" / "probe_cpu_stage3.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def descendants(processes, known):
    result = set(known)
    while True:
        updated = result | {pid for pid, parent in processes if parent in result}
        if updated == result:
            return result
        result = updated


@pytest.fixture
def fake_offline(monkeypatch):
    offline = SimpleNamespace(descendant_pids=descendants)
    monkeypatch.setitem(sys.modules, "voicebar.stt.offline", offline)
    return offline


def test_real_worker_must_be_current_launcher_descendant(fake_offline):
    monitor = SimpleNamespace(processes=lambda: [(5, 1), (10, 5), (11, 10), (99, 77)])
    assert probe.verify_worker_pid(10, 5, monitor) == [5, 10, 11]
    for pid in (99, 12, os.getpid(), False):
        with pytest.raises(RuntimeError):
            probe.verify_worker_pid(pid, 5, monitor)


def test_deadline_closes_only_verified_job_and_owned_launcher(fake_offline, monkeypatch, tmp_path):
    clock = [100.0]
    actions = []
    pid = 10
    records = [
        {"kind": "hello", "pid": pid},
        {"kind": "ready", "pid": pid, "backend": "qwen", "device": "cpu", "audit_canaries": 4,
         "audio_seconds": 2.0},
    ]

    class Stdin:
        closed = False

        def write(self, value):
            actions.append(("write", value))
            if value == "GO\n":
                clock[0] += 33

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class Process:
        pid = 5
        returncode = None

        def __init__(self):
            self.stdin = Stdin()
            self.stdout = io.StringIO("".join(probe.PREFIX + json.dumps(record) + "\n" for record in records))
            self.stderr = io.StringIO("")

        def poll(self):
            return self.returncode

        def terminate(self):
            actions.append(("terminate_launcher", self.pid))
            self.returncode = -1

        def kill(self):
            raise AssertionError("Graceful owned-launcher termination should suffice in this fake.")

        def wait(self, timeout):
            assert self.returncode is not None
            return self.returncode

    class Job:
        def __init__(self, worker_pid):
            actions.append(("job_assign", worker_pid))

        def close(self):
            actions.append(("job_close", pid))

    process = Process()
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(probe, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    fake_offline.NativeEndpointMonitor = lambda: SimpleNamespace(processes=lambda: [(5, 1), (pid, 5), (99, 77)])
    fake_offline.WorkerJob = Job
    args = SimpleNamespace(root=tmp_path, backend="qwen", session=probe.DEFAULT_SESSION,
                           init_timeout=120, inference_timeout=30, output=tmp_path / "report.json")
    assert probe.run_probe(args) == 1
    report = json.loads(args.output.read_text())
    assert report["timed_out"] and report["timeout_phase"] == "inference"
    assert not report["completed"] and report["exact_plans"] == 0 and report["probe_denominator"] == 1
    assert report["accuracy_over_40"] is None and report["p95_eos_ms"] is None
    assert report["inference_deadline_seconds"] == 32
    assert actions == [("job_assign", pid), ("write", "LOAD\n"), ("write", "GO\n"),
                       ("job_close", pid), ("terminate_launcher", 5)]

