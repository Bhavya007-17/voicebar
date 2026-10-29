"""Bounded client for Fusion's documented local desktop MCP endpoint.

The transport never starts a server, follows a redirect, uses a proxy, or
accepts a URL. Discovery does not authorize tools. Only independently reviewed,
fixed tool/argument pairs in APPROVED_CALLS may be invoked.
"""
from __future__ import annotations

import http.client
import json
import math
import socket
import threading
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType

ENDPOINT = "http://127.0.0.1:27182/mcp"
HOST = "127.0.0.1"
PORT = 27182
PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = frozenset({"2025-03-26", "2025-06-18"})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOOLS = 256
MAX_PAGES = 8

# These exact scripts use APIs inspected in the installed adsk/core.py and the
# FusionSketchCreate id from StringTable/en-US/NaFusionUI10.xml. No argument is
# taken from speech, YAML or a model. Mutation verification is performed below.
_WINDOW_GUARD = """import ctypes
import os
from ctypes import wintypes

def _require_foreground():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    foreground = user32.GetForegroundWindow
    foreground.argtypes, foreground.restype = [], wintypes.HWND
    window_pid = user32.GetWindowThreadProcessId
    window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    window_pid.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    if not window_pid(foreground(), ctypes.byref(pid)) or pid.value != os.getpid():
        raise RuntimeError("Fusion is no longer the foreground process")

"""
_NEW_DESIGN = _WINDOW_GUARD + """import adsk.core
import json

def run(_context: str):
    app = adsk.core.Application.get()
    _require_foreground()
    document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    if document is None:
        raise RuntimeError("Fusion did not create a design")
    print(json.dumps({"voicebar_command": "new_design", "document_name": document.name}))
"""
_CREATE_SKETCH = _WINDOW_GUARD + """import adsk.core
import json

def run(_context: str):
    app = adsk.core.Application.get()
    command = app.userInterface.commandDefinitions.itemById("FusionSketchCreate")
    _require_foreground()
    if command is None or not command.execute():
        raise RuntimeError("Fusion could not start Create Sketch")
    print(json.dumps({"voicebar_command": "create_sketch", "started": True}))
"""
_FIT_VIEW = _WINDOW_GUARD + """import adsk.core
import json

def run(_context: str):
    viewport = adsk.core.Application.get().activeViewport
    _require_foreground()
    if viewport is None or not viewport.fit():
        raise RuntimeError("Fusion could not fit the view")
    print(json.dumps({"voicebar_command": "fit_view", "fitted": True}))
"""
_CAMERA_STATE = """import adsk.core
import json

def run(_context: str):
    viewport = adsk.core.Application.get().activeViewport
    if viewport is None:
        raise RuntimeError("Fusion has no active viewport")
    camera = viewport.camera
    print(json.dumps({"voicebar_query": "camera_state", "eye": [camera.eye.x, camera.eye.y, camera.eye.z], "target": [camera.target.x, camera.target.y, camera.target.z], "view_extents": camera.viewExtents}))
"""
ALLOWED_COMMANDS = frozenset({"new_design", "create_sketch", "fit_view"})
_COMMAND_SCRIPTS = MappingProxyType({"new_design": _NEW_DESIGN,
                                   "create_sketch": _CREATE_SKETCH,
                                   "fit_view": _FIT_VIEW})


def _script_arguments(script, *, read_only=False):
    object_value = {"script": script}
    if read_only:
        object_value["readOnly"] = True
    return {"featureType": "script", "object": object_value}


def _canonical(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


# Inspected from this machine's owned Fusion window on 2026-10-12; exact native
# metadata is preserved in docs/evidence/stage7-fusion-mcp-discovery.json.
# 'document/open' LISTS open documents; it does not open a file. Execute accepts
# ONLY the exact three scripts above and the exact read-only camera query.
# Cloud queries, arbitrary APIs/scripts, save/close and undo are omitted.
APPROVED_CALLS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "fusion_mcp_read": ('{"queryType":"activeCommand"}',
                        '{"operation":"open","queryType":"document"}'),
    "fusion_mcp_execute": tuple(_canonical(_script_arguments(script))
                                for script in _COMMAND_SCRIPTS.values()) + (
                                    _canonical(_script_arguments(_CAMERA_STATE, read_only=True)),),
})


class FusionMCPError(RuntimeError):
    """A connection or tool request that must produce no further action."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FusionMCPError("Duplicate MCP response field")
        result[key] = value
    return result


def _json(raw):
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                FusionMCPError("Non-finite MCP JSON value")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FusionMCPError("Invalid MCP JSON response") from error
    if not isinstance(result, dict):
        raise FusionMCPError("MCP response must be an object")
    return result


def validate_tool_call(name, arguments):
    """Accept only hardcoded names with an exact approved, fixed argument set."""
    if not isinstance(name, str) or name not in APPROVED_CALLS:
        raise FusionMCPError("Fusion MCP tool is not in the fixed allowlist")
    if not isinstance(arguments, dict):
        raise FusionMCPError("Fusion MCP arguments must be a fixed JSON object")
    # Canonical JSON compares types exactly and rejects runtime objects, cycles
    # and non-finite values. Immutable policy strings cannot be mutated by callers.
    try:
        encoded = json.dumps(arguments, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as error:
        raise FusionMCPError("Fusion MCP arguments must be fixed JSON values") from error
    if encoded not in APPROVED_CALLS[name]:
        raise FusionMCPError("Fusion MCP arguments are not a fixed approved call")
    return json.loads(encoded)


def validate_command(command):
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise FusionMCPError("Fusion MCP command is not in the fixed allowlist")
    return command


def _cancel_predicate(cancel):
    if cancel is None or callable(cancel):
        return cancel
    if isinstance(cancel, threading.Event):
        return cancel.is_set
    raise FusionMCPError("Fusion MCP cancellation must be an Event or callable")


def _is_cancelled(cancel):
    try:
        return cancel is not None and bool(cancel())
    except Exception as error:  # uncertain authorization must revoke the request.
        raise FusionMCPError("Fusion MCP cancellation check failed") from error


def _tool_data(result):
    """Read one JSON payload without treating tool text as code or instructions."""
    if isinstance(result.get("structuredContent"), dict):
        value = result["structuredContent"]
    else:
        content = result.get("content")
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise FusionMCPError("Fusion MCP did not return one verifiable JSON payload")
        value = _json(content[0]["text"])
    if value.get("success") is False or "error" in value:
        detail = value.get("message") or value.get("error") or "No server detail"
        raise FusionMCPError(f"Fusion MCP operation reported failure: {str(detail)[:500]}")
    return value


def _script_record(result, field, expected):
    pending = [(_tool_data(result), 0)]
    for _ in range(32):
        if not pending:
            break
        value, depth = pending.pop()
        if isinstance(value, dict):
            if value.get("success") is False or "error" in value:
                raise FusionMCPError("Fusion MCP script reported failure")
            if value.get(field) == expected:
                return value
            if depth < 4:
                pending.extend((value[key], depth + 1) for key in ("output", "result", "text", "message")
                               if key in value)
        elif isinstance(value, str):
            try:
                pending.append((_json(value.strip()), depth + 1))
            except FusionMCPError:
                pass
        elif isinstance(value, list) and depth < 4:
            pending.extend((item, depth + 1) for item in value[:16])
    raise FusionMCPError("Fusion MCP script completion is unconfirmed")


class FusionMCPClient:
    """One operation deadline across initialization, discovery and one call.

    ``guard`` must query the native foreground HWND and reject a lost target.
    It is called before every request, including immediately before tool use.
    ``cancel`` is a thread-safe predicate, such as a capture-generation check.
    A failed or timed-out call is never retried: the server may have acted.
    """

    def __init__(self, *, timeout_s=3.0):
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 10):
            raise ValueError("Fusion MCP timeout must be finite and from 0 to 10 seconds")
        self.timeout_s = float(timeout_s)
        self._session_id = None
        self._version = PROTOCOL_VERSION
        self._next_id = 1
        self._lock = threading.Lock()

    @staticmethod
    def _check(deadline, guard, cancel):
        if _is_cancelled(cancel):
            raise FusionMCPError("Fusion MCP request cancelled")
        if time.monotonic() >= deadline:
            raise FusionMCPError("Fusion MCP deadline expired; result is unconfirmed")
        if guard is not None and guard() is False:
            raise FusionMCPError("Fusion is no longer the foreground window")
        if _is_cancelled(cancel):
            raise FusionMCPError("Fusion MCP request cancelled")
        if time.monotonic() >= deadline:
            raise FusionMCPError("Fusion MCP deadline expired; result is unconfirmed")

    def _request(self, message, deadline, guard, cancel):
        self._check(deadline, guard, cancel)
        connection = http.client.HTTPConnection(HOST, PORT,
                                                timeout=deadline - time.monotonic())
        # HTTPConnection.send normally reconnects after close(). A watchdog
        # revocation must never reopen the connection and send a late mutation.
        connection.auto_open = False
        complete = threading.Event()
        raw_socket = [None]

        def interrupt():
            # Shutdown interrupts a blocked header/body read as well as slow
            # trickle responses that would defeat a per-recv socket timeout.
            sock = raw_socket[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        def watch():
            while not complete.wait(max(0, min(.02, deadline - time.monotonic()))):
                try:
                    revoked = _is_cancelled(cancel)
                except FusionMCPError:
                    revoked = True
                if time.monotonic() >= deadline or revoked:
                    interrupt()
                    return

        watcher = threading.Thread(target=watch, name="voicebar-fusion-deadline", daemon=True)
        watcher.start()
        try:
            connection.connect()
            raw_socket[0] = connection.sock
            self._check(deadline, guard, cancel)
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            if self._session_id is not None:
                headers["Mcp-Session-Id"] = self._session_id
            if message["method"] != "initialize":
                headers["MCP-Protocol-Version"] = self._version
            connection.request("POST", PATH, body=json.dumps(message, allow_nan=False).encode("utf-8"),
                               headers=headers)
            response = connection.getresponse()
            self._check(deadline, guard, cancel)
            if "id" not in message:
                if response.status != 202:
                    raise FusionMCPError(f"Fusion MCP notification returned HTTP {response.status}")
                return None
            if response.status != 200:
                # HTTPConnection does not follow Location or consult proxies.
                raise FusionMCPError(f"Fusion MCP returned HTTP {response.status}")
            session_id = response.getheader("Mcp-Session-Id")
            if session_id is not None:
                if (not 1 <= len(session_id) <= 256
                        or any(not 0x21 <= ord(char) <= 0x7e for char in session_id)):
                    raise FusionMCPError("Invalid Fusion MCP session id")
                if message["method"] != "initialize" and session_id != self._session_id:
                    raise FusionMCPError("Fusion MCP session changed unexpectedly")
                self._session_id = session_id
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/json", "text/event-stream"}:
                raise FusionMCPError("Unsupported Fusion MCP response content type")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError as error:
                    raise FusionMCPError("Invalid Fusion MCP content length") from error
                if not 0 <= size <= MAX_RESPONSE_BYTES:
                    raise FusionMCPError("Fusion MCP response exceeded size limit")
            raw, pending = bytearray(), bytearray()
            while True:
                self._check(deadline, guard, cancel)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise FusionMCPError("Fusion MCP response exceeded size limit")
                if content_type == "text/event-stream":
                    pending.extend(chunk)
                    # SSE records end at a blank line; normalize CRLF first.
                    pending[:] = pending.replace(b"\r\n", b"\n")
                    while b"\n\n" in pending:
                        event, _, rest = pending.partition(b"\n\n")
                        pending[:] = rest
                        data = b"\n".join(line[5:].lstrip(b" ") for line in event.split(b"\n")
                                          if line.startswith(b"data:"))
                        if data:
                            result = _json(data)
                            if result.get("id") == message["id"]:
                                result = self._result(result, message["id"])
                                self._check(deadline, guard, cancel)
                                return result
                            if "method" in result and "id" not in result:
                                continue  # bounded server notification; never instructions
                            raise FusionMCPError("Unexpected Fusion MCP stream message")
            self._check(deadline, guard, cancel)
            if content_type == "text/event-stream":
                raise FusionMCPError("Fusion MCP stream ended without a result")
            result = self._result(_json(raw), message["id"])
            self._check(deadline, guard, cancel)
            return result
        except (OSError, http.client.HTTPException) as error:
            raise FusionMCPError("Local Fusion MCP unavailable or timed out; result is unconfirmed") from error
        finally:
            complete.set()
            connection.close()
            watcher.join(.1)

    @staticmethod
    def _result(message, request_id):
        if (message.get("jsonrpc") != "2.0" or message.get("id") != request_id
                or "error" in message or not isinstance(message.get("result"), dict)):
            raise FusionMCPError("Fusion MCP returned an error or unmatched result")
        result = message["result"]
        if result.get("isError") is not None and result["isError"] is not False:
            raise FusionMCPError("Fusion MCP tool reported failure")
        return result

    def _rpc(self, method, params, deadline, guard, cancel, *, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = self._next_id
            self._next_id += 1
        return self._request(message, deadline, guard, cancel)

    def _initialize(self, deadline, guard, cancel):
        self._session_id = None
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "voicebar", "version": "0.1.0"},
        }, deadline, guard, cancel)
        if (result.get("protocolVersion") not in SUPPORTED_VERSIONS
                or not isinstance(result.get("capabilities"), dict)
                or "tools" not in result["capabilities"]):
            raise FusionMCPError("Fusion MCP did not negotiate supported tools")
        self._version = result["protocolVersion"]
        self._rpc("notifications/initialized", None, deadline, guard, cancel, notify=True)
        return result.get("serverInfo", {})

    def _tools(self, deadline, guard, cancel):
        tools, cursor, names, cursors = [], None, set(), set()
        for _ in range(MAX_PAGES):
            result = self._rpc("tools/list", {} if cursor is None else {"cursor": cursor},
                               deadline, guard, cancel)
            page = result.get("tools")
            if not isinstance(page, list) or len(tools) + len(page) > MAX_TOOLS:
                raise FusionMCPError("Invalid or oversized Fusion MCP tool list")
            for tool in page:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not tool["name"] or tool["name"] in names
                        or not isinstance(tool.get("inputSchema"), dict)):
                    raise FusionMCPError("Invalid or duplicate Fusion MCP tool metadata")
                names.add(tool["name"])
                tools.append(tool)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise FusionMCPError("Invalid Fusion MCP pagination cursor")
            cursors.add(cursor)
        raise FusionMCPError("Fusion MCP tool list exceeded page limit")

    def discover(self, *, guard: Callable[[], None] | None = None, cancel=None):
        """Read-only diagnostic; returned schemas never expand the allowlist."""
        cancel = _cancel_predicate(cancel)
        with self._lock:
            deadline = time.monotonic() + self.timeout_s
            server = self._initialize(deadline, guard, cancel)
            tools = self._tools(deadline, guard, cancel)
            return {"endpoint": ENDPOINT, "server": server, "tools": tools,
                    "allowed_tools": sorted(APPROVED_CALLS)}

    def call(self, name, arguments, *, guard: Callable[[], None], cancel=None):
        cancel = _cancel_predicate(cancel)
        arguments = validate_tool_call(name, arguments)
        if not callable(guard):
            raise FusionMCPError("Fusion MCP execution requires a native foreground guard")
        with self._lock:
            deadline = time.monotonic() + self.timeout_s
            self._initialize(deadline, guard, cancel)
            tools = self._tools(deadline, guard, cancel)
            if not any(tool["name"] == name for tool in tools):
                raise FusionMCPError("Approved Fusion MCP tool is unavailable in this session")
            return self._rpc("tools/call", {"name": name, "arguments": arguments},
                             deadline, guard, cancel)

    def call_command(self, command, *, guard: Callable[[], None], cancel=None):
        """Run one fixed native operation once, then verify actual Fusion state."""
        cancel = _cancel_predicate(cancel)
        command = validate_command(command)
        if not callable(guard):
            raise FusionMCPError("Fusion MCP execution requires a native foreground guard")
        with self._lock:
            deadline = time.monotonic() + self.timeout_s
            self._initialize(deadline, guard, cancel)
            tools = self._tools(deadline, guard, cancel)
            names = {tool["name"] for tool in tools}
            if not {"fusion_mcp_read", "fusion_mcp_execute"} <= names:
                raise FusionMCPError("Required fixed Fusion MCP tools are unavailable")

            def invoke(name, arguments):
                arguments = validate_tool_call(name, arguments)
                return self._rpc("tools/call", {"name": name, "arguments": arguments},
                                 deadline, guard, cancel)

            before = None
            if command == "new_design":
                before = _tool_data(invoke("fusion_mcp_read", {
                    "queryType": "document", "operation": "open"})).get("results")
                if not isinstance(before, list):
                    raise FusionMCPError("Fusion open-document baseline is unavailable")
            result = invoke("fusion_mcp_execute", _script_arguments(_COMMAND_SCRIPTS[command]))
            record = _script_record(result, "voicebar_command", command)
            if command == "new_design":
                state = _tool_data(invoke("fusion_mcp_read", {
                    "queryType": "document", "operation": "open"}))
                documents = state.get("results")
                if (not isinstance(documents, list) or len(documents) != len(before) + 1
                        or not any(isinstance(doc, dict) and doc.get("isActive") is True
                                   and doc.get("name") == record.get("document_name")
                                   for doc in documents)):
                    raise FusionMCPError("New Fusion design state is unconfirmed; do not retry automatically")
            elif command == "create_sketch":
                state = _tool_data(invoke("fusion_mcp_read", {"queryType": "activeCommand"}))
                active = state.get("activeCommand")
                command_id = state.get("commandId")
                if isinstance(active, dict):
                    command_id = active.get("id")
                if record.get("started") is not True or command_id != "FusionSketchCreate":
                    raise FusionMCPError("Fusion Create Sketch state is unconfirmed; do not retry automatically")
            else:
                state = _script_record(invoke("fusion_mcp_execute", _script_arguments(
                    _CAMERA_STATE, read_only=True)), "voicebar_query", "camera_state")
                if (record.get("fitted") is not True
                        or any(not isinstance(state.get(key), list) or len(state[key]) != 3
                               or any(type(number) not in (int, float) or not math.isfinite(number)
                                      for number in state[key]) for key in ("eye", "target"))
                        or type(state.get("view_extents")) not in (int, float)
                        or not math.isfinite(state["view_extents"])):
                    raise FusionMCPError("Fusion fitted camera state is unconfirmed; do not retry automatically")
            return {"command": command, "verified": True, "result": record, "state": state}


def call_command(command, *, guard, cancel=None):
    """Runtime entry point; three-second total deadline, no write retries."""
    return FusionMCPClient(timeout_s=3).call_command(command, guard=guard, cancel=cancel)
