"""Cold-load STT proof with Python socket refusal and native endpoint sampling.

This is a development proof harness, not a firewall or a security sandbox. The
physical network remains enabled. Native extensions can bypass Python audit
events, and polling cannot see endpoints that appear entirely between samples.
"""

from __future__ import annotations

import argparse
import ctypes as C
import ipaddress
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PREFIX = "VOICEBAR_OFFLINE "
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
               "HF_HUB_DISABLE_UPDATE_CHECK": "1", "DISABLE_TELEMETRY": "1",
               "DO_NOT_TRACK": "1", "TRANSFORMERS_OFFLINE": "1"}
DWORD, BOOL, HANDLE = C.c_uint32, C.c_int32, C.c_void_p


class OfflineViolation(RuntimeError):
    """A proof child attempted network use or monitoring could not verify it."""


class SocketAuditPolicy:
    """Installed only in a disposable worker; hooks cannot be removed."""

    def __init__(self, emit=None):
        self.phase = "canary"
        self.events: list[dict] = []
        self.emit = emit

    def __call__(self, event, arguments):
        deny = event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                         "socket.gethostbyaddr", "socket.getnameinfo"}
        if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
            host = arguments[0]
            try:
                if isinstance(host, bytes):
                    host = host.decode("ascii")
                # Names such as localhost can invoke DNS; only numeric loopback
                # resolution is permitted, and all subsequent connects fail.
                deny = not ipaddress.ip_address(host).is_loopback
            except (ValueError, TypeError, UnicodeError):
                deny = True
        if deny:
            record = {"event": event, "phase": self.phase, "monotonic": time.perf_counter()}
            self.events.append(record)
            if self.emit is not None:
                self.emit({"kind": "socket_refused", **record})
            raise OfflineViolation(f"Offline proof refused {event} before its system call")

    def install_and_check(self):
        sys.addaudithook(self)
        # Inject documented events directly: no socket or external connection is
        # created to test the guard. Tests additionally exercise real CPython
        # socket entry points with a loopback address and confirm refusal.
        cases = (("socket.connect", (None, ("203.0.113.1", 9))),
                 ("socket.bind", (None, ("127.0.0.1", 0))),
                 ("socket.getaddrinfo", ("offline-canary.invalid", 9, 0, 0, 0)),
                 ("socket.sendto", (None, ("203.0.113.1", 9))))
        for event, arguments in cases:
            try:
                sys.audit(event, *arguments)
            except OfflineViolation:
                continue
            raise OfflineViolation(f"Socket audit canary did not refuse {event}")
        self.phase = "runtime"
        return len(cases)


class TCP4(C.Structure):
    _fields_ = [(name, DWORD) for name in
                ("state", "local", "local_port", "remote", "remote_port", "pid")]


class TCP6(C.Structure):
    _fields_ = [("local", C.c_ubyte * 16), ("local_scope", DWORD), ("local_port", DWORD),
                ("remote", C.c_ubyte * 16), ("remote_scope", DWORD), ("remote_port", DWORD),
                ("state", DWORD), ("pid", DWORD)]


class UDP4(C.Structure):
    _fields_ = [("local", DWORD), ("local_port", DWORD), ("pid", DWORD)]


class UDP6(C.Structure):
    _fields_ = [("local", C.c_ubyte * 16), ("local_scope", DWORD), ("local_port", DWORD),
                ("pid", DWORD)]


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("size", DWORD), ("usage", DWORD), ("pid", DWORD), ("heap", C.c_size_t),
                ("module", DWORD), ("threads", DWORD), ("parent", DWORD),
                ("priority", C.c_int32), ("flags", DWORD), ("exe", C.c_wchar * 260)]


class JOB_BASIC_LIMITS(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", DWORD),
                ("minimum_working_set", C.c_size_t), ("maximum_working_set", C.c_size_t),
                ("active_processes", DWORD), ("affinity", C.c_size_t),
                ("priority", DWORD), ("scheduling", DWORD)]


class JOB_EXTENDED_LIMITS(C.Structure):
    _fields_ = [("basic", JOB_BASIC_LIMITS), ("io_counters", C.c_uint64 * 6),
                ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class WorkerJob:
    """Contain only the verified worker and future descendants for cleanup.

    No network, resource, security, or UI limits are configured. This unnamed
    job's sole limit kills its own processes when this parent closes the handle.
    """

    def __init__(self, pid):
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": (HANDLE, HANDLE, C.c_wchar_p),
            "SetInformationJobObject": (BOOL, HANDLE, C.c_int, HANDLE, DWORD),
            "AssignProcessToJobObject": (BOOL, HANDLE, HANDLE),
            "OpenProcess": (HANDLE, DWORD, BOOL, DWORD),
            "CloseHandle": (BOOL, HANDLE),
        }
        for name, signature in signatures.items():
            function = getattr(self.kernel, name)
            function.restype, function.argtypes = signature[0], list(signature[1:])
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OfflineViolation(f"Cannot create worker cleanup job ({C.get_last_error()})")
        try:
            limits = JOB_EXTENDED_LIMITS()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
                raise OfflineViolation(f"Cannot configure worker cleanup job ({C.get_last_error()})")
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process:
                raise OfflineViolation(f"Cannot open verified worker for cleanup ({C.get_last_error()})")
            try:
                if not self.kernel.AssignProcessToJobObject(self.handle, process):
                    raise OfflineViolation(f"Cannot contain verified worker in cleanup job ({C.get_last_error()})")
            finally:
                self.kernel.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise OfflineViolation(f"Cannot close worker cleanup job ({C.get_last_error()})")
            self.handle = None


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    family: int
    pid: int
    local: str
    local_port: int
    remote: str | None = None
    remote_port: int | None = None
    state: int | None = None


def _port(value):
    return int.from_bytes(int(value).to_bytes(4, "little")[:2], "big")


def _address(value, family):
    raw = int(value).to_bytes(4, "little") if family == 2 else bytes(value)
    return str(ipaddress.ip_address(raw))


def endpoint_reason(endpoint: Endpoint) -> str | None:
    if endpoint.protocol == "udp":
        return "UDP endpoint"
    if endpoint.state == 2:
        return "TCP listening socket"
    if endpoint.state == 1:  # CLOSED is a fictional state with no connection.
        return None
    try:
        remote = ipaddress.ip_address(endpoint.remote)
        loopback = remote.is_loopback or bool(
            getattr(remote, "ipv4_mapped", None) and remote.ipv4_mapped.is_loopback
        )
    except (ValueError, TypeError):
        return "TCP endpoint has an unrecognizable remote address"
    return "TCP loopback connection (Stage 3 permits no clients)" if loopback else "Nonloopback TCP connection"


def descendant_pids(processes, known):
    """Retain previously observed descendants even after their parent exits."""
    discovered = set(known)
    while True:
        additions = {pid for pid, parent in processes if parent in discovered}
        if additions <= discovered:
            return discovered
        discovered.update(additions)


class NativeEndpointMonitor:
    """Read-only IP Helper tables and Toolhelp process ancestry, without admin."""

    def __init__(self):
        if os.name != "nt":
            raise OfflineViolation("Native offline monitoring requires Windows")
        self.ip = C.WinDLL("iphlpapi", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        for name in ("GetExtendedTcpTable", "GetExtendedUdpTable"):
            function = getattr(self.ip, name)
            function.restype = DWORD
            function.argtypes = [HANDLE, C.POINTER(DWORD), BOOL, DWORD, C.c_int, DWORD]
        signatures = {
            "CreateToolhelp32Snapshot": (HANDLE, DWORD, DWORD),
            "Process32FirstW": (BOOL, HANDLE, C.POINTER(PROCESSENTRY32W)),
            "Process32NextW": (BOOL, HANDLE, C.POINTER(PROCESSENTRY32W)),
            "CloseHandle": (BOOL, HANDLE),
        }
        for name, signature in signatures.items():
            function = getattr(self.kernel, name)
            function.restype, function.argtypes = signature[0], list(signature[1:])

    def processes(self):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, HANDLE(-1).value):
            raise OfflineViolation(f"Process snapshot failed ({C.get_last_error()})")
        values = []
        try:
            entry = PROCESSENTRY32W()
            entry.size = C.sizeof(entry)
            if not self.kernel.Process32FirstW(snapshot, C.byref(entry)):
                raise OfflineViolation(f"Process enumeration failed ({C.get_last_error()})")
            while True:
                values.append((int(entry.pid), int(entry.parent)))
                if not self.kernel.Process32NextW(snapshot, C.byref(entry)):
                    if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise OfflineViolation(f"Process enumeration failed ({C.get_last_error()})")
                    return values
        finally:
            self.kernel.CloseHandle(snapshot)

    @staticmethod
    def _table(function, family, table_class, row_type):
        size = DWORD()
        status = function(None, C.byref(size), False, family, table_class, 0)
        if status not in (0, 122):
            raise OfflineViolation(f"Endpoint table size query failed ({status})")
        for _ in range(5):
            if not 4 <= size.value <= 128 * 1024 * 1024:
                raise OfflineViolation("Endpoint table returned an invalid buffer size")
            buffer = C.create_string_buffer(size.value)
            status = function(buffer, C.byref(size), False, family, table_class, 0)
            if status == 122:
                continue  # Table can grow between the required sized calls.
            if status:
                raise OfflineViolation(f"Endpoint table query failed ({status})")
            count = DWORD.from_buffer(buffer).value
            # All four OWNER_PID row types have DWORD alignment; table[0] is at 4.
            required = 4 + count * C.sizeof(row_type)
            if required > min(len(buffer), size.value):
                raise OfflineViolation("Endpoint table was truncated")
            return [row_type.from_buffer_copy(buffer, 4 + index * C.sizeof(row_type))
                    for index in range(count)]
        raise OfflineViolation("Endpoint table changed repeatedly during sampling")

    def endpoints(self, pids):
        values = []
        for family, tcp, udp in ((2, TCP4, UDP4), (23, TCP6, UDP6)):
            for row in self._table(self.ip.GetExtendedTcpTable, family, 5, tcp):
                if row.pid in pids:
                    values.append(Endpoint("tcp", family, int(row.pid), _address(row.local, family),
                                           _port(row.local_port), _address(row.remote, family),
                                           _port(row.remote_port), int(row.state)))
            for row in self._table(self.ip.GetExtendedUdpTable, family, 1, udp):
                if row.pid in pids:
                    values.append(Endpoint("udp", family, int(row.pid), _address(row.local, family),
                                           _port(row.local_port)))
        return values

    def sample(self, known):
        pids = descendant_pids(self.processes(), known)
        endpoints = self.endpoints(pids)
        return pids, endpoints


def read_manifest(path: Path):
    path = Path(path).resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        items = document["items"]
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a nonempty list")
        ids = set()
        result = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TypeError(f"item {index} must be an object")
            identifier = item.get("id", str(index))
            if not isinstance(identifier, (str, int)) or str(identifier) in ids:
                raise ValueError(f"item {index} needs a unique string/integer id")
            ids.add(str(identifier))
            wav = item.get("wav")
            if not isinstance(wav, str) or not wav:
                raise ValueError(f"item {identifier} needs a WAV path")
            resolved = (path.parent / wav).resolve()
            if not resolved.is_file():
                raise ValueError(f"item {identifier} WAV is missing: {resolved}")
            last_app = item.get("last_app")
            if last_app is not None and not isinstance(last_app, str):
                raise ValueError(f"item {identifier} last_app must be text or null")
            result.append({**item, "id": str(identifier), "wav": str(resolved)})
        return result
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OfflineViolation(f"Invalid corpus manifest: {error}") from error


def _emit(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)


def worker(root: Path, backend_name: str, manifest: Path, device: str) -> int:
    os.environ.update(OFFLINE_ENV)
    policy = SocketAuditPolicy(emit=_emit)
    canaries = policy.install_and_check()
    _emit({"kind": "ready", "pid": os.getpid(), "audit_canaries": canaries,
           "environment": OFFLINE_ENV})
    if sys.stdin.readline().strip() != "GO":
        raise OfflineViolation("Parent did not start monitored inference")
    # Keep imports after the hook and the parent monitoring handshake.
    from ..catalog import load_catalog
    from ..parser import parse
    from ..schema import validate_plan
    from . import load_backend, model_assets
    from .base import transcribe_wav

    items = read_manifest(manifest)
    loaded = load_catalog(root)
    assets = model_assets(backend_name, device)
    verifying = time.perf_counter()
    if assets:
        from .models import require_model
        for name in assets:
            require_model(root, name, verify_hashes=True)
    validation_ms = (time.perf_counter() - verifying) * 1000
    began = time.perf_counter()
    backend = load_backend(backend_name, root, loaded.catalog, device=device)
    load_ms = (time.perf_counter() - began) * 1000
    _emit({"kind": "loaded", "backend": backend_name, "device": device,
           "cold_load_ms": load_ms, "asset_validation_ms": validation_ms,
           "sha256_verified_assets": list(assets)})
    errors, count = [], 0
    try:
        for item in items:
            began = time.perf_counter()
            try:
                output = transcribe_wav(backend, Path(item["wav"]))
                if not isinstance(output, dict) or not isinstance(output.get("text"), str):
                    raise OfflineViolation("Backend result has no transcript text")
                parsing = time.perf_counter()
                plan = parse(output["text"], loaded.catalog, last_app=item.get("last_app"))
                if plan is not None:
                    validate_plan(plan)
                row = {**output, "kind": "result", "id": item["id"], "wav": item["wav"],
                       "plan": plan, "parse_ms": (time.perf_counter() - parsing) * 1000,
                       "total_ms": (time.perf_counter() - began) * 1000,
                       "expected_plan": item.get("expected_plan"),
                       "exact_plan": plan == item.get("expected_plan")}
            except (RuntimeError, OSError, ValueError, TypeError) as error:
                errors.append({"id": item["id"], "error": str(error)})
                row = {"kind": "result", "id": item["id"], "wav": item["wav"],
                       "error": str(error), "total_ms": (time.perf_counter() - began) * 1000}
            _emit(row)
            count += 1
    finally:
        backend.close()
    attempts = [event for event in policy.events if event["phase"] == "runtime"]
    _emit({"kind": "summary", "backend": backend_name, "device": device,
           "items": len(items), "results": count, "cold_load_ms": load_ms,
           "errors": errors, "runtime_socket_attempts": attempts,
           "audit_canaries": canaries, "ok": not errors and not attempts})
    return 0 if not errors and not attempts else 1


def _reader(stream, channel, messages):
    try:
        for line in stream:
            messages.put((channel, line))
    finally:
        messages.put((channel, None))


def prove(root: Path, backend: str, corpus_manifest: Path, *, device="cpu", interval_ms=10,
          timeout=1800, monitor=None, progress=None) -> dict:
    """Load one backend once, replay every WAV, and return honest offline evidence."""
    if not 0 < interval_ms <= 10:
        raise ValueError("Sampling interval must be positive and at most 10 ms")
    if timeout <= 0:
        raise ValueError("Proof timeout must be positive")
    root, corpus_manifest = Path(root).resolve(), Path(corpus_manifest).resolve()
    expected_items = read_manifest(corpus_manifest)
    monitor = monitor if monitor is not None else NativeEndpointMonitor()
    environment = {**os.environ, **OFFLINE_ENV, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    command = [sys.executable, "-m", "voicebar.stt.offline", "worker", "--root", str(root),
               "--backend", backend, "--manifest", str(corpus_manifest), "--device", device]
    began = time.perf_counter()
    process = subprocess.Popen(command, cwd=root, env=environment, shell=False,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    messages = queue.Queue()
    readers = [threading.Thread(target=_reader, args=(stream, channel, messages), daemon=True)
               for channel, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for thread in readers:
        thread.start()
    report = {"backend": backend, "device": device, "command": command, "pid": process.pid,
              "environment": OFFLINE_ENV, "physical_network_disabled": False,
              "samples": [], "violations": [], "records": [], "stdout_log": [], "stderr": [],
              "limitations": [
                  "Python audit hooks cover CPython socket entry points; native extensions and subprocesses can bypass them.",
                  "Native endpoint/process polling can miss endpoints or descendant processes born and gone between samples.",
                  "No adapter, firewall, or machine-wide network settings were changed; this is not physical network isolation.",
                  "An observed descendant PID is retained; very rapid PID reuse can make attribution conservative.",
              ]}
    known, eof, ready, released = {process.pid}, set(), False, False
    worker_pid = None
    result_count = 0
    job, contained = None, False
    last_sample, max_gap = None, 0.0
    failure = None

    def drain():
        nonlocal ready, worker_pid, result_count
        while True:
            try:
                channel, line = messages.get_nowait()
            except queue.Empty:
                return
            if line is None:
                eof.add(channel)
            elif channel == "stdout" and line.startswith(PREFIX):
                value = json.loads(line[len(PREFIX):])
                if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
                    raise OfflineViolation("Worker emitted an invalid protocol record")
                report["records"].append(value)
                if value["kind"] == "result":
                    result_count += 1
                    if progress is not None:
                        progress(result_count, len(expected_items))
                if value["kind"] == "ready":
                    candidate = value.get("pid")
                    if (ready or not isinstance(candidate, int) or candidate <= 0
                            or value.get("audit_canaries") != 4):
                        raise OfflineViolation("Worker audit readiness was not verified")
                    worker_pid = candidate
                    ready = True
            else:
                report["stderr" if channel == "stderr" else "stdout_log"].append(line.rstrip("\r\n"))

    try:
        while True:
            sampled = time.perf_counter()
            if last_sample is not None:
                max_gap = max(max_gap, sampled - last_sample)
            last_sample = sampled
            known, endpoints = monitor.sample(known)
            observed = {"elapsed_ms": (sampled - began) * 1000,
                        "pids": sorted(known), "endpoints": [asdict(item) for item in endpoints]}
            report["samples"].append(observed)
            for endpoint in endpoints:
                reason = endpoint_reason(endpoint)
                if reason:
                    report["violations"].append({**asdict(endpoint), "reason": reason,
                                                  "elapsed_ms": observed["elapsed_ms"]})
            drain()
            if report["violations"]:
                raise OfflineViolation("A worker/descendant network endpoint was observed")
            # Windows venv python.exe is a redirector: its interpreter can have
            # a different PID. Verify that PID through the native process tree.
            if ready and not released and worker_pid in known:
                if os.name == "nt":
                    job = WorkerJob(worker_pid)
                    contained = True
                process.stdin.write("GO\n")
                process.stdin.flush()
                process.stdin.close()
                released = True
            if process.poll() is not None:
                break
            if time.perf_counter() - began > timeout:
                raise OfflineViolation(f"Offline proof exceeded {timeout:g} seconds")
            remaining = interval_ms / 1000 - (time.perf_counter() - sampled)
            if remaining > 0:
                time.sleep(remaining)  # Monitoring cadence, never an inference race workaround.
    except (RuntimeError, OSError, ValueError) as error:
        failure = str(error)
    finally:
        if job is not None:
            try:
                job.close()
            except (RuntimeError, OSError) as error:
                failure = failure or str(error)
        if process.poll() is None:
            # This is only the child created above, never an application target.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=5)
        try:
            drain()
        except (RuntimeError, ValueError) as error:
            failure = failure or str(error)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    attempts = [record for record in report["records"]
                if record["kind"] == "socket_refused" and record.get("phase") == "runtime"]
    summaries = [record for record in report["records"] if record["kind"] == "summary"]
    results = [record for record in report["records"] if record["kind"] == "result"]
    valid_summary = (len(summaries) == 1 and summaries[0].get("ok") is True
                     and summaries[0].get("items") == len(expected_items)
                     and summaries[0].get("results") == len(expected_items)
                     and len(results) == len(expected_items)
                     and {row.get("id") for row in results} == {row["id"] for row in expected_items})
    report.update({"returncode": process.returncode, "elapsed_seconds": time.perf_counter() - began,
                   "requested_interval_ms": interval_ms, "max_sample_gap_ms": max_gap * 1000,
                   "sample_count": len(report["samples"]), "observed_pids": sorted(known),
                   "worker_pid": worker_pid,
                   "worker_job_contained": contained,
                   "monitoring_error": failure, "ready_before_load": released,
                   "audit_runtime_attempts": attempts,
                   "ok": bool(not failure and released and process.returncode == 0
                              and not report["violations"] and not attempts and valid_summary)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    child = subcommands.add_parser("worker")
    child.add_argument("--root", type=Path, required=True)
    child.add_argument("--backend", required=True)
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    try:
        return worker(args.root.resolve(), args.backend, args.manifest.resolve(), args.device)
    except (RuntimeError, OSError, ValueError, TypeError) as error:
        _emit({"kind": "fatal", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
