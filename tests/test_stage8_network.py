"""App socket proof uses only owned processes, WAV input and recorded plans."""

import json
import os
import socket
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar import network_proof
from voicebar.stt import models


def test_psutil_monitor_retains_orphans_and_reads_endpoint_tables_once_per_sample(monkeypatch):
    import psutil

    traversed, queries = [], []
    descendants = {1: [2, 3], 2: [3], 3: []}

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return float(self.pid)

        def children(self, *, recursive):
            assert recursive is True
            traversed.append(self.pid)
            if descendants[self.pid] is None:
                raise psutil.NoSuchProcess(self.pid)
            return [Process(pid) for pid in descendants[self.pid]]

    def connections(*, kind):
        assert kind == "inet"
        queries.append(kind)
        return [SimpleNamespace(pid=pid, family=socket.AF_INET, type=socket.SOCK_STREAM,
                                status="ESTABLISHED", laddr=("127.0.0.1", 10000),
                                raddr=("127.0.0.1", 11434)) for pid in [3, 99]]

    monkeypatch.setattr(psutil, "Process", Process)
    monkeypatch.setattr(psutil, "net_connections", connections)
    monitor = network_proof.PsutilMonitor(1)
    assert monitor.sample()[0] == [1, 2, 3]
    assert traversed == [1]
    descendants[1] = None  # An earlier parent exits; its observed child survives.
    pids, endpoints = monitor.sample()
    assert pids == [1, 2, 3]
    assert [row["pid"] for row in endpoints] == [3]
    assert traversed == [1, 1, 2]
    assert len(queries) == 2


@pytest.mark.parametrize("kind,status,remote,reason", [
    (socket.SOCK_STREAM, "ESTABLISHED", ["127.0.0.1", 11434], None),
    (socket.SOCK_STREAM, "LISTEN", [], "listening"),
    (socket.SOCK_STREAM, "ESTABLISHED", ["::1", 11434], "outside"),
    (socket.SOCK_STREAM, "SYN_SENT", ["203.0.113.1", 443], "outside"),
    (socket.SOCK_DGRAM, "NONE", [], "UDP"),
])
def test_psutil_endpoint_policy_has_exact_ipv4_loopback_exception(kind, status, remote, reason):
    actual = network_proof.endpoint_reason({"type": int(kind), "status": status, "remote": remote})
    assert actual is None if reason is None else reason in actual


def test_runtime_environment_disables_updates_telemetry_and_lazy_downloads(monkeypatch):
    for key in network_proof.OFFLINE_ENV:
        monkeypatch.setenv(key, "0")
    models.offline_environment()
    assert all(os.environ[key] == "1" for key in network_proof.OFFLINE_ENV)


def test_onnx_telemetry_helper_calls_installed_opt_out(monkeypatch):
    calls = []
    import onnxruntime
    monkeypatch.setattr(onnxruntime, "disable_telemetry_events", lambda: calls.append("disabled"))
    models.disable_onnx_telemetry()
    assert calls == ["disabled"]


def test_direct_silero_disables_telemetry_before_creating_session(monkeypatch, tmp_path):
    from voicebar.stt.parakeet import Silero

    calls = []

    def options():
        calls.append("options")
        assert calls == ["disabled", "options"]
        return SimpleNamespace()

    def session(*args, **kwargs):
        calls.append("session")
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        disable_telemetry_events=lambda: calls.append("disabled"),
        SessionOptions=options, InferenceSession=session))
    Silero(tmp_path)
    assert calls == ["disabled", "options", "session"]


def test_direct_parakeet_disables_telemetry_before_creating_session(monkeypatch, tmp_path):
    from voicebar.stt import parakeet

    calls = []

    def options():
        calls.append("options")
        assert calls == ["disabled", "options"]
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        disable_telemetry_events=lambda: calls.append("disabled"), SessionOptions=options))
    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=lambda *a, **kw: SimpleNamespace()))
    monkeypatch.setattr(parakeet, "Silero", lambda directory: SimpleNamespace())
    parakeet.ParakeetBackend(tmp_path, tmp_path, None)
    assert calls == ["disabled", "options"]


def test_import_disables_tracking_without_blocking_explicit_setup_downloads():
    completed = subprocess.run(
        [sys.executable, "-c", ("import os,voicebar; "
         "assert os.environ['HF_HUB_DISABLE_UPDATE_CHECK']=='1'; "
         "assert os.environ['HF_HUB_DISABLE_TELEMETRY']=='1'; "
         "assert 'HF_HUB_OFFLINE' not in os.environ")],
        cwd=Path(__file__).resolve().parents[1],
        env={key: value for key, value in os.environ.items()
             if key not in {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"}},
        capture_output=True, text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr


def test_socket_audit_refuses_external_connections_and_bind_before_syscall():
    code = """
import json,sys
from voicebar.network_proof import LocalSocketPolicy
from voicebar.stt.offline import OfflineViolation
policy=LocalSocketPolicy()
assert policy.install_and_check()==4
sys.audit('socket.connect', None, ('127.0.0.1',11434))
sys.audit('socket.getaddrinfo', '127.0.0.1',11434,0,0,0)
for event,args in [('socket.connect',(None,('203.0.113.1',443))),
                   ('socket.connect',(None,('::1',11434,0,0))),
                   ('socket.bind',(None,('127.0.0.1',0))),
                   ('socket.getaddrinfo',('::1',11434,0,0,0))]:
    try: sys.audit(event,*args)
    except OfflineViolation: pass
    else: raise AssertionError(event)
print(json.dumps({'refused':len(policy.events)}))
"""
    completed = subprocess.run([sys.executable, "-c", code],
                               cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"refused": 8}


def replay_project(root):
    (root / "config").mkdir()
    (root / "config/settings.yaml").write_text(
        "names: [computer]\nmode: name\nengines:\n  google: https://www.google.com/search?q={q}\n",
        encoding="utf-8")
    (root / "config/aliases.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config/sites.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config/pinned.yaml").write_text("[]\n", encoding="utf-8")
    (root / ".cache").mkdir()
    (root / ".cache/startapps.json").write_text("[]\n", encoding="utf-8")
    # Non-silent PCM passes the real held-input silence guard. Transcript choice
    # is deterministic only in this unit proof; the separate script loads STT.
    pcm = np.full(1600, 1800, dtype="<i2").tobytes()
    with wave.open(str(root / "owned.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm)
    manifest = root / "session.json"
    manifest.write_text(json.dumps({"items": [
        {"id": "named", "wav": "owned.wav", "text": "computer go to sleep"},
        {"id": "wake", "wav": "owned.wav", "text": "computer wake up"},
        {"id": "unknown", "wav": "owned.wav", "text": "this is conversation"},
    ]}), encoding="utf-8")
    return manifest


def test_app_process_and_children_open_no_listener_or_external_connection(tmp_path):
    manifest = replay_project(tmp_path)
    report = network_proof.prove(tmp_path, manifest, backend="fixture", device="cpu",
                                 fixture=True, gui=False, timeout=30)
    assert report["ok"], json.dumps(report, default=str)
    assert report["sample_count"] >= 2
    assert report["ready_before_load"]
    assert report["worker_pid"] in report["observed_pids"]
    assert not report["violations"]
    assert report["physical_network_disabled"] is False
    results = [row for row in report["records"] if row["kind"] == "result"]
    assert [row["id"] for row in results] == ["named", "wake", "unknown"]
    assert results[0]["decision"]["plan"] == [{"action": "mode", "op": "sleep"}]
    assert results[1]["decision"]["plan"] == [{"action": "mode", "op": "wake"}]
    assert results[2]["decision"]["plan"] is None
    summary = next(row for row in report["records"] if row["kind"] == "summary")
    assert not summary["stream_open"]
    assert not summary["runtime_socket_attempts"]


def test_offline_claim_refuses_still_enabled_physical_adapter(tmp_path, monkeypatch):
    manifest = replay_project(tmp_path)
    monkeypatch.setattr(network_proof, "physical_adapters", lambda: [{"Name": "Wi-Fi", "Status": "Up"}])
    monkeypatch.setattr(network_proof.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("App started despite enabled physical network"))
    with pytest.raises(network_proof.OfflineViolation, match="still enabled"):
        network_proof.prove(tmp_path, manifest, expect_offline=True)


def test_observed_forbidden_endpoint_fails_the_proof_before_replay(tmp_path, monkeypatch):
    manifest = replay_project(tmp_path)

    class ForbiddenMonitor:
        def __init__(self, pid):
            self.pid = pid

        def sample(self):
            return [self.pid], [{"pid": self.pid, "family": int(socket.AF_INET),
                                 "type": int(socket.SOCK_STREAM), "status": "LISTEN",
                                 "local": ["127.0.0.1", 19000], "remote": []}]

    monkeypatch.setattr(network_proof, "PsutilMonitor", ForbiddenMonitor)
    report = network_proof.prove(tmp_path, manifest, fixture=True, gui=False, timeout=10)
    assert not report["ok"]
    assert report["violations"][0]["reason"] == "listening socket"
    assert not report["ready_before_load"]
    assert not any(row["kind"] == "loaded" for row in report["records"])
    assert report["returncode"] is not None


def test_monitor_initialization_failure_cannot_be_reported_as_local_success(tmp_path, monkeypatch):
    manifest = replay_project(tmp_path)

    def unavailable(pid):
        raise PermissionError("process socket inspection unavailable")

    monkeypatch.setattr(network_proof, "PsutilMonitor", unavailable)
    report = network_proof.prove(tmp_path, manifest, fixture=True, gui=False, timeout=10)
    assert not report["ok"]
    assert "inspection unavailable" in report["monitoring_error"]
    assert not report["ready_before_load"]
    assert report["returncode"] is not None
