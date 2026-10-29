"""Offline evidence fails closed without making external test connections."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from voicebar.stt import offline


def test_ip_helper_rows_have_sdk_widths_and_network_byte_order():
    assert ctypes.sizeof(offline.TCP4) == 24
    assert ctypes.sizeof(offline.TCP6) == 56
    assert ctypes.sizeof(offline.UDP4) == 12
    assert ctypes.sizeof(offline.UDP6) == 28
    assert offline.TCP6.state.offset == 48
    assert offline._address(0x0100007F, 2) == "127.0.0.1"
    assert offline._address((ctypes.c_ubyte * 16)(*[0] * 15, 1), 23) == "::1"
    assert offline._port(0x5000) == 80
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(offline.JOB_BASIC_LIMITS) == 64
        assert ctypes.sizeof(offline.JOB_EXTENDED_LIMITS) == 144


@pytest.mark.parametrize("endpoint,reason", [
    (offline.Endpoint("udp", 2, 40, "127.0.0.1", 12), "UDP"),
    (offline.Endpoint("tcp", 23, 40, "::1", 12, "::", 0, 2), "listening"),
    (offline.Endpoint("tcp", 2, 40, "127.0.0.1", 12, "127.0.0.1", 13, 5), "loopback"),
    (offline.Endpoint("tcp", 23, 40, "::1", 12, "::ffff:127.0.0.1", 13, 5), "loopback"),
    (offline.Endpoint("tcp", 2, 40, "192.0.2.1", 12, "203.0.113.1", 13, 3), "Nonloopback"),
])
def test_socket_policy_rejects_every_active_stage3_endpoint(endpoint, reason):
    assert reason in offline.endpoint_reason(endpoint)


def test_descendants_include_nested_children_and_survive_parent_exit():
    assert offline.descendant_pids([(30, 20), (20, 10), (40, 99)], {10}) == {10, 20, 30}
    assert offline.descendant_pids([(50, 30)], {10, 20, 30}) == {10, 20, 30, 50}


def test_native_monitor_refuses_non_windows_before_loading_dll(monkeypatch):
    monkeypatch.setattr(offline.os, "name", "posix")
    monkeypatch.setattr(offline.C, "WinDLL", lambda *args, **kwargs: pytest.fail("DLL loaded"), raising=False)
    with pytest.raises(offline.OfflineViolation, match="requires Windows"):
        offline.NativeEndpointMonitor()


def test_table_growth_is_retried_and_truncation_is_rejected():
    calls = []

    def query(buffer, size, order, family, table_class, reserved):
        calls.append(buffer is None)
        length = ctypes.cast(size, ctypes.POINTER(offline.DWORD)).contents
        if buffer is None:
            length.value = 4
            return 122
        if len(calls) == 2:
            length.value = 28
            return 122
        offline.DWORD.from_buffer(buffer).value = 1
        row = offline.TCP4.from_buffer(buffer, 4)
        row.pid = 75
        return 0

    rows = offline.NativeEndpointMonitor._table(query, 2, 5, offline.TCP4)
    assert [row.pid for row in rows] == [75]
    assert len(calls) == 3

    def truncated(buffer, size, *args):
        if buffer is None:
            ctypes.cast(size, ctypes.POINTER(offline.DWORD)).contents.value = 4
            return 122
        offline.DWORD.from_buffer(buffer).value = 2
        return 0

    with pytest.raises(offline.OfflineViolation, match="truncated"):
        offline.NativeEndpointMonitor._table(truncated, 2, 5, offline.TCP4)


def test_audit_canary_and_actual_socket_entries_are_refused_before_syscalls():
    script = """
import json, socket
from voicebar.stt.offline import OfflineViolation, SocketAuditPolicy
policy = SocketAuditPolicy()
assert policy.install_and_check() == 4
blocked = []
with socket.socket() as client:
    for name, call in [('connect', lambda: client.connect(('127.0.0.1', 9))),
                       ('bind', lambda: client.bind(('127.0.0.1', 0)))]:
        try:
            call()
        except OfflineViolation:
            blocked.append(name)
        else:
            raise AssertionError('audit hook failed before socket system call')
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
    try:
        client.sendto(b'canary', ('127.0.0.1', 9))
    except OfflineViolation:
        blocked.append('sendto')
    else:
        raise AssertionError('sendto audit hook failed')
try:
    socket.getaddrinfo('offline-canary.invalid', 9)
except OfflineViolation:
    blocked.append('getaddrinfo')
else:
    raise AssertionError('DNS audit hook failed')
print(json.dumps({'blocked': blocked, 'events': len(policy.events)}))
"""
    completed = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                               shell=False, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == {
        "blocked": ["connect", "bind", "sendto", "getaddrinfo"], "events": 8}


def fake_project(tmp_path, *, attempted_network=False, slow=False):
    package = tmp_path / "voicebar"
    stt = package / "stt"
    stt.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copyfile(offline.__file__, stt / "offline.py")
    (package / "catalog.py").write_text(
        "from types import SimpleNamespace\ndef load_catalog(root): return SimpleNamespace(catalog=None)\n")
    (package / "schema.py").write_text("def validate_plan(plan): pass\n")
    (package / "parser.py").write_text(
        "def parse(text, catalog, last_app=None):\n"
        "    return [{'action':'switch_app','app':last_app}] if last_app else [{'action':'press','keys':'enter'}]\n")
    (stt / "base.py").write_text(
        "import time\ndef transcribe_wav(backend, path):\n"
        + ("    time.sleep(30)\n" if slow else "")
        + "    return {'text': 'go', 'decode_ms': 1, 'events': []}\n")
    body = (
        "import os, sys\n"
        "class Backend:\n"
        "    def close(self): pass\n"
        "def model_assets(name, device):\n"
        "    assert name == 'fake'\n"
        "    return ('fake-asset',)\n"
        "def load_backend(name, root, catalog, device='cpu'):\n"
        "    assert all(os.environ.get(key) == '1' for key in ['HF_HUB_OFFLINE','HF_HUB_DISABLE_TELEMETRY','DO_NOT_TRACK'])\n"
        "    with (root / 'load-count.txt').open('a') as output: output.write('load\\n')\n"
    )
    if attempted_network:
        body += "    sys.audit('socket.connect', None, ('203.0.113.1', 9))\n"
    body += "    return Backend()\n"
    (stt / "__init__.py").write_text(body)
    (stt / "models.py").write_text(
        "def require_model(root, name, *, verify_hashes):\n"
        "    assert name == 'fake-asset' and verify_hashes is True\n"
        "    with (root / 'asset-checks.txt').open('a') as output: output.write(name + '\\n')\n"
        "    return root\n")
    (tmp_path / "first.wav").write_bytes(b"fake audio read only by the stub backend")
    (tmp_path / "second.wav").write_bytes(b"fake audio read only by the stub backend")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"items": [
        {"id": "first", "wav": "first.wav", "expected_plan": [{"action": "press", "keys": "enter"}]},
        {"id": "second", "wav": "second.wav", "last_app": "Notepad",
         "expected_plan": [{"action": "switch_app", "app": "Notepad"}]},
    ]}))
    return manifest


class NoEndpoints:
    def sample(self, known):
        if os.name == "nt":
            # The Windows venv launcher spawns the real interpreter. Test its
            # ancestry using the real read-only process table while endpoints
            # remain fake for deterministic success/failure coverage.
            known = offline.descendant_pids(offline.NativeEndpointMonitor().processes(), known)
        return known, []


def test_proof_loads_once_and_covers_full_corpus_with_reference_context(tmp_path):
    manifest = fake_project(tmp_path)
    report = offline.prove(tmp_path, "fake", manifest, monitor=NoEndpoints(), timeout=10)
    assert report["ok"], json.dumps(report)
    assert (tmp_path / "load-count.txt").read_text() == "load\n"
    assert (tmp_path / "asset-checks.txt").read_text() == "fake-asset\n"
    loaded = [row for row in report["records"] if row["kind"] == "loaded"]
    assert loaded[0]["sha256_verified_assets"] == ["fake-asset"]
    results = [row for row in report["records"] if row["kind"] == "result"]
    assert len(results) == 2 and all(row["exact_plan"] for row in results)
    assert report["sample_count"] >= 2
    assert report["ready_before_load"]
    assert report["physical_network_disabled"] is False
    if os.name == "nt":
        assert report["worker_job_contained"]


def test_proof_captures_loader_network_attempt_and_fails(tmp_path):
    manifest = fake_project(tmp_path, attempted_network=True)
    report = offline.prove(tmp_path, "fake", manifest, monitor=NoEndpoints(), timeout=10)
    assert not report["ok"]
    assert report["returncode"] == 1
    assert report["audit_runtime_attempts"], json.dumps(report)
    assert report["audit_runtime_attempts"][0]["event"] == "socket.connect"
    assert any(row["kind"] == "fatal" for row in report["records"])


def test_native_monitor_failure_cannot_be_reported_as_offline_success(tmp_path):
    manifest = fake_project(tmp_path)

    class FailedMonitor:
        def sample(self, known):
            raise offline.OfflineViolation("native query failed")

    report = offline.prove(tmp_path, "fake", manifest, monitor=FailedMonitor(), timeout=10)
    assert not report["ok"]
    assert report["monitoring_error"] == "native query failed"
    assert report["returncode"] is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows cleanup job required")
def test_timed_out_proof_closes_actual_interpreter_and_launcher(tmp_path):
    manifest = fake_project(tmp_path, slow=True)
    report = offline.prove(tmp_path, "fake", manifest, monitor=NoEndpoints(), timeout=1)
    assert not report["ok"]
    assert report["worker_job_contained"]
    assert "exceeded" in report["monitoring_error"]
    processes = dict(offline.NativeEndpointMonitor().processes())
    assert report["worker_pid"] not in processes
    assert report["pid"] not in processes


@pytest.mark.skipif(os.name != "nt", reason="Windows IP Helper API required")
def test_native_monitor_reads_all_four_tables_for_this_process():
    monitor = offline.NativeEndpointMonitor()
    pids, endpoints = monitor.sample({os.getpid()})
    assert os.getpid() in pids
    assert all(endpoint.pid in pids and endpoint.family in (2, 23) for endpoint in endpoints)
