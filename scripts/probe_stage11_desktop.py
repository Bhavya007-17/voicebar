"""Replay failed spoken transcripts through the gate and real owned Calculator.

This tests text-to-action, not physical microphone recognition. The Qt capture
timer runs while the command worker resolves; only our Calculator may be captured.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from probe_stage10 import OwnedCaptureNative, calculator_display, run_action

from voicebar.conversation import ConversationGate
from voicebar.executor import Context
from voicebar.executor.app_actions import foreground_app
from voicebar.executor.uia import Automation
from voicebar.listening import Chimes, GateSession
from voicebar.resolver.vision import VisionSelector
from voicebar.screen import label_alternatives, resolve_control
from voicebar.screen_frames import ScreenFrames

ROOT = Path(__file__).resolve().parents[1]


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--output", type=Path, default=ROOT / "docs/evidence/stage11-desktop.json")
    args = arguments.parse_args()
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from voicebar.ui.screen_capture import ScreenCapture

    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    selector = VisionSelector()
    report = {"time": datetime.now(UTC).isoformat(), "physical_microphone_tested": False,
              "screenshots_persisted": False, "decisions": [], "verified": False}
    warmed_at = time.perf_counter()
    report["warm"] = {"ok": selector.warm(), "ms": (time.perf_counter() - warmed_at) * 1000}
    if not report["warm"]["ok"]:
        raise RuntimeError(f"Local vision warmup failed: {selector.last_outcome}")
    server = selector.backend._request("/api/ps", timeout_s=2)
    report["model_residency"] = [{key: row.get(key) for key in
                                  ("name", "expires_at", "size_vram", "size")}
                                 for row in server.get("models", [])]
    capture = None
    window = None
    cancelled = threading.Event()
    with Context(ROOT, owned={}, persist=False, cancel=cancelled) as ctx:
        # Keep the real atomic ownership journal in a unique test directory.
        # Workspace file replacement contention is independently reproduced;
        # the journal and all ownership checks remain enabled here.
        journal_directory = Path(tempfile.mkdtemp(prefix="voicebar-stage11-desktop-"))
        ctx.ownership_record = journal_directory / "owned.json"
        report["ownership_record"] = str(ctx.ownership_record)
        if ctx.apps.find("Calculator"):
            raise RuntimeError("Existing unowned Calculator was left untouched; probe refused")
        try:
            run_action(ctx, [{"action": "open_app", "app": "Calculator"}])
            window = ctx.target("Calculator")
            ctx.required_foreground = (window.hwnd, window.pid)
            ctx.automation = Automation()
            expected = [None]
            frames = ScreenFrames()
            errors = []

            def execute_owned(plan):
                if len(plan) != 1 or not expected[0](plan[0]):
                    raise RuntimeError("Unexpected action in owned-window replay; no input delivered")
                return run_action(ctx, plan)

            def grounded(text):
                result = resolve_control(text, ROOT, ctx.loaded, settings=ctx.settings,
                    frames=frames, selector=selector, generation=session.generation,
                    authorized=lambda: session.capture_allowed,
                    required_foreground=(window.hwnd, window.pid))
                if result is None:
                    return None
                plan, identity = result
                if identity != (window.hwnd, window.pid):
                    raise RuntimeError("Screen resolution escaped the owned Calculator")
                ctx.required_foreground = identity
                return plan

            session = GateSession(ConversationGate(ctx.loaded.catalog, ["computer"]),
                execute=execute_owned, clock=time.perf_counter,
                chimes=Chimes(play=lambda: None), grounded_resolver=grounded,
                foreground_app=lambda: foreground_app(ROOT, ctx.loaded, settings=ctx.settings),
                resolver=lambda *args: None)
            adapter = OwnedCaptureNative(ctx, window)
            controller = SimpleNamespace(screen_frames=frames, closed=threading.Event(),
                screen_capture_state=lambda: (bool(adapter.foreground()), session.generation),
                screen_capture_error=errors.append)
            capture = ScreenCapture(controller, application, native=adapter)
            report["owned_window"] = {"hwnd": window.hwnd, "pid": window.pid}

            def calculate(expression):
                return lambda action: action == {"action": "app_action", "app": "Calculator",
                                                  "name": "calculate:" + expression}

            # Calculator exposes a Memory tab only at some window sizes. Read
            # the owned UI first: a missing tab with several Memory buttons
            # must clarify, never silently choose Memory add as the old probe did.
            memory = ctx.automation.find(window.hwnd, {"name": "Memory"})
            if len(memory) == 1 and memory[0]["role"] == "tabitem":
                memory_reason = "run"
                memory_action = {"action": "app_action", "app": "active",
                                 "name": "click_role:tabitem:" + memory[0]["name"]}
            else:
                memory = label_alternatives("memory", ctx.automation.snapshot(window.hwnd))
                if len(memory) < 2:
                    raise RuntimeError("Owned Calculator lacks the expected Memory ambiguity or tab")
                memory_reason, memory_action = "unresolved", None
            report["memory_observation"] = {"controls": memory, "expected_reason": memory_reason}
            memory_tabs = [row for row in memory if row["role"] == "tabitem"]
            clarified_memory_action = ({"action": "app_action", "app": "active",
                                        "name": "click_role:tabitem:" + memory_tabs[0]["name"]}
                                       if len(memory_tabs) == 1 else None)
            report["memory_tab_clarification_available"] = clarified_memory_action is not None
            seven_action = {"action": "app_action", "app": "active", "name": "click_role:button:Seven"}
            cases = [
                ("Um, computer.", lambda action: False, "armed", None),
                ("Type two plus three.", calculate("two plus three"), "run", "5"),
                ("Ah, clear calculator.", lambda action: action == {
                    "action": "app_action", "app": "Calculator", "name": "clear"}, "run", "0"),
                ("Uh, do six plus seven.", calculate("six plus seven"), "run", "13"),
                ("Uh, click on memory.", lambda action: action == memory_action, memory_reason, None),
                ("Click on the button Seven.", lambda action: action == seven_action, "run", "7"),
                ("Click the number seven.", lambda action: action == seven_action, "run", "77"),
                ("Click this.", lambda action: False, "unresolved", None),
            ]
            if clarified_memory_action is not None:
                cases.insert(5, ("Click the tab Memory.", lambda action: action == clarified_memory_action,
                                 "run", None))
            with ThreadPoolExecutor(max_workers=1) as worker:
                for phrase, check, reason, display in cases:
                    # Calculator's accessible Memory label includes live status
                    # text. Reobserve after preceding arithmetic, before this
                    # command; the startup label is not a stable identity.
                    if phrase == "Uh, click on memory.":
                        current = ctx.automation.find(window.hwnd, {"name": "Memory"})
                        if len(current) == 1 and current[0]["role"] == "tabitem":
                            wanted = {"action": "app_action", "app": "active",
                                      "name": "click_role:tabitem:" + current[0]["name"]}
                            reason = "run"
                        else:
                            current = label_alternatives("memory", ctx.automation.snapshot(window.hwnd))
                            if len(current) < 2:
                                raise RuntimeError("Memory command no longer has the expected tab or ambiguity")
                            wanted, reason = None, "unresolved"
                        check = lambda action, wanted=wanted: action == wanted
                        report["memory_observation_at_command"] = {
                            "controls": current, "expected_reason": reason}
                    elif phrase == "Click the tab Memory.":
                        current = label_alternatives("memory", ctx.automation.snapshot(window.hwnd), "tabitem")
                        if len(current) != 1:
                            raise RuntimeError("Memory clarification no longer has one observed tab")
                        wanted = {"action": "app_action", "app": "active",
                                  "name": "click_role:tabitem:" + current[0]["name"]}
                        check = lambda action, wanted=wanted: action == wanted
                    expected[0] = check
                    capture.capture()
                    now = time.perf_counter()
                    future = worker.submit(session.hear, phrase, start=now, end=now,
                        speech_end=now, generation=session.generation)
                    deadline = now + 15
                    loop = QEventLoop()
                    poll = QTimer()
                    poll.setInterval(10)
                    poll.timeout.connect(lambda loop=loop, future=future, deadline=deadline:
                                         loop.quit() if future.done()
                                         or time.perf_counter() > deadline else None)
                    poll.start()
                    if not future.done():
                        loop.exec()
                    poll.stop()
                    if not future.done():
                        cancelled.set()
                        session.control(enabled=False)
                        raise RuntimeError("Command replay exceeded its deadline")
                    decision = future.result()
                    report["decisions"].append(decision)
                    if decision["reason"] != reason or (reason == "run" and not all(
                            result.get("ok") for result in decision.get("results", []))):
                        raise RuntimeError(f"Replay failed for {phrase!r}: {decision.get('detail') or decision.get('results')}")
                    if display is not None:
                        decision["observed_display"] = calculator_display(ctx, window, display)
            report["verified"] = not any(errors)
            report["capture_errors"] = errors
        except (RuntimeError, OSError, ValueError) as error:
            report["error"] = str(error)
        finally:
            if capture is not None:
                capture.close()
            if window is not None and ctx.native.is_window(window.hwnd, window.pid):
                ctx.cancel = None  # Worker has joined; allow owned-window cleanup.
                ctx.required_foreground = None
                report["cleanup"] = run_action(ctx, [{"action": "close_app", "app": "Calculator"}])
                report["owned_window_closed"] = not ctx.native.is_window(window.hwnd, window.pid)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
