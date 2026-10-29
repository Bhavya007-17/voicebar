"""Owned-window Stage 10 smoke probe; screenshots exist only in memory.

Run with the native desktop available. Existing Calculator or Notepad windows
make that section unverified; this script never activates or closes them.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from voicebar.executor import Context, execute
from voicebar.executor.app_actions import foreground_app
from voicebar.executor.uia import Automation, unique
from voicebar.parser import parse
from voicebar.resolver.vision import VisionSelector
from voicebar.screen import visual_candidates
from voicebar.screen_frames import ScreenFrames

ROOT = Path(__file__).resolve().parents[1]


def run_action(ctx, plan):
    if not plan:
        raise RuntimeError("The probe phrase did not produce a plan")
    results = execute(plan, context=ctx)
    if len(results) != len(plan) or not all(item["ok"] for item in results):
        raise RuntimeError(f"Owned action failed: {results}")
    return results


def same_foreground(ctx, window):
    ctx.guard(window)
    if ctx.native.foreground() != window.hwnd:
        raise RuntimeError("The owned probe window lost focus; probe refused")


def calculator_display(ctx, window, expected):
    def observed():
        same_foreground(ctx, window)
        rows = ctx.automation.find(window.hwnd, {"automation_id": "CalculatorResults"})
        result = unique(rows, {"automation_id": "CalculatorResults"})
        return result["name"] if result["name"].strip().casefold() == f"display is {expected}" else None

    return ctx.native.wait(observed, 3.0, "owned Calculator result")


class OwnedCaptureNative:
    """Capture cannot observe a different window even if focus changes mid-call."""

    def __init__(self, ctx, window):
        self.ctx, self.window = ctx, window

    def foreground(self):
        if (self.ctx.native.foreground() == self.window.hwnd
                and self.ctx.native.is_window(self.window.hwnd, self.window.pid)):
            return self.window.hwnd
        return 0

    def _pid_tid(self, hwnd):
        if hwnd != self.window.hwnd:
            raise RuntimeError("Capture requested an unowned window")
        self.ctx.guard(self.window)
        return self.ctx.native._pid_tid(hwnd)

    def is_window(self, hwnd, pid):
        return ((hwnd, pid) == (self.window.hwnd, self.window.pid)
                and self.ctx.native.is_window(hwnd, pid))

    def shutdown(self):
        pass  # The surrounding execution context owns the native adapter.


def visual_calculator(ctx, window, application, selector):
    from voicebar.ui.screen_capture import ScreenCapture

    same_foreground(ctx, window)
    run_action(ctx, [{"action": "press", "keys": "esc"}])
    calculator_display(ctx, window, "0")
    frames = ScreenFrames()
    errors = []
    adapter = OwnedCaptureNative(ctx, window)
    controller = SimpleNamespace(
        screen_frames=frames, screen_capture_state=lambda: (bool(adapter.foreground()), 1),
        screen_capture_error=errors.append, closed=threading.Event())
    capture = ScreenCapture(controller, application, native=adapter)
    capture.timer.stop()
    try:
        capture.capture()
        frame = frames.get(window.hwnd, window.pid, 1)
        if frame is None:
            raise RuntimeError(f"Owned screen capture failed: {errors}")
        candidates = visual_candidates(ctx.automation.snapshot(window.hwnd))
        if not 1 <= len(candidates) <= 96:
            raise RuntimeError("Owned Calculator did not expose a bounded candidate set")
        same_foreground(ctx, window)
        began = time.perf_counter()
        choice = selector.select("click the number seven", frame.png, candidates, timeout_s=5.0)
        selection_ms = (time.perf_counter() - began) * 1000
        if type(choice) is not int or not 1 <= choice <= len(candidates):
            raise RuntimeError(f"Vision abstained: {selector.last_outcome}; selection_ms={selection_ms:.1f}")
        selected = candidates[choice - 1]
        if (selected["role"] != "button"
                or " ".join(selected["name"].split()).casefold() not in {"seven", "7"}):
            raise RuntimeError(
                f"Vision chose {selected!r} instead of the observed Seven button; "
                f"selection_ms={selection_ms:.1f}; no click ran")
        same_foreground(ctx, window)
        if visual_candidates(ctx.automation.snapshot(window.hwnd)) != candidates:
            raise RuntimeError("Owned Calculator controls changed during vision selection")
        result = run_action(ctx, [{"action": "app_action", "app": "active",
                                   "name": f"click_role:{selected['role']}:{selected['name']}"}])
        display = calculator_display(ctx, window, "7")
        return {"verified": True, "request": "click the number seven",
                "candidate_count": len(candidates), "candidates": candidates,
                "selected": selected, "selection_ms": selection_ms,
                "selector_outcome": selector.last_outcome, "execution": result,
                "display": display, "pixels_persisted": False}
    finally:
        frames.clear()
        capture.close()
        capture.timer.deleteLater()


def cleanup_owned(ctx, window, app, report):
    if window is None or not ctx.native.is_window(window.hwnd, window.pid):
        return
    try:
        ctx.required_foreground = None
        ctx.guard(window)
        if app == "Notepad":
            if not report.get("blank_verified", False):
                raise RuntimeError("Owned Notepad was never proven blank; preserved without clearing or closing")
            text = ctx.native.read_text(window.hwnd)
            if text not in {"", "2+3"}:
                raise RuntimeError("Owned Notepad contains unexpected text; preserved without clearing or closing")
            if text:
                ctx.guard(window)
                ctx.native.clear_text(window.hwnd)
        report["cleanup"] = run_action(ctx, [{"action": "close_app", "app": app}])
        report["closed_owned_window"] = not ctx.native.is_window(window.hwnd, window.pid)
    except (OSError, RuntimeError, ValueError) as error:
        report["cleanup_error"] = str(error)
        report["verified"] = False


def probe_app(app, application, selector):
    report = {"app": app, "verified": False}
    window = None
    with Context(ROOT, owned={}, persist=False) as ctx:
        if ctx.apps.find(app):
            report["not_verified"] = "Existing unowned app; left untouched"
            return report
        try:
            report["launch"] = run_action(ctx, [{"action": "open_app", "app": app}])
            window = ctx.target(app)
            report["owned_window"] = {"hwnd": window.hwnd, "pid": window.pid}
            ctx.required_foreground = (window.hwnd, window.pid)
            same_foreground(ctx, window)
            if app == "Notepad":
                def editor_ready():
                    same_foreground(ctx, window)
                    try:
                        return True, ctx.native.read_text(window.hwnd)
                    except RuntimeError as error:
                        if "no readable standard edit control" in str(error):
                            return None
                        raise

                _, original = ctx.wait(editor_ready, "owned Notepad's readable editor")
                if original:
                    run_action(ctx, [{"action": "press", "keys": "ctrl+n"}])
                    _, original = ctx.wait(
                        lambda: (True, "") if ctx.native.read_text(window.hwnd) == "" else None,
                        "new blank document in owned Notepad")
                if original != "":
                    raise RuntimeError("New owned Notepad was not blank; no typing ran")
                report["blank_verified"] = True
                plan = parse("type 2+3", ctx.loaded.catalog, active_app=foreground_app(
                    ROOT, ctx.loaded, settings=ctx.settings))
                if plan != [{"action": "type_text", "text": "2+3", "submit": False}]:
                    raise RuntimeError("Notepad expression was not parsed as literal typing")
                report["typing"] = run_action(ctx, plan)
                actual = ctx.native.wait(lambda: ctx.native.read_text(window.hwnd) == "2+3",
                                         3.0, "owned Notepad literal text")
                report["literal_readback_verified"] = bool(actual)
            else:
                ctx.automation = Automation()
                report["calculations"] = []
                for phrase in ("type two plus three", "add two plus three"):
                    active = foreground_app(ROOT, ctx.loaded, settings=ctx.settings)
                    if active != "Calculator":
                        raise RuntimeError("Actual foreground app was not identified as Calculator")
                    plan = parse(phrase, ctx.loaded.catalog, active_app=active)
                    expected = [{"action": "app_action", "app": "Calculator",
                                 "name": "calculate:two plus three"}]
                    if plan != expected:
                        raise RuntimeError("Calculator arithmetic did not produce its context action")
                    result = run_action(ctx, plan)
                    report["calculations"].append({"phrase": phrase, "plan": plan,
                        "execution": result, "display": calculator_display(ctx, window, "5")})
                report["vision"] = visual_calculator(ctx, window, application, selector)
            report["verified"] = True
        except (OSError, RuntimeError, ValueError) as error:
            report["error"] = str(error)
        finally:
            if window is None and ctx.owned:
                adopted = ctx.apps.find(app, ctx.owned)
                if len(adopted) == 1:
                    window = adopted[0]
            cleanup_owned(ctx, window, app, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/stage10-owned-probe.json")
    parser.add_argument("--model", default="qwen3-vl:2b-instruct")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--calculator-only", action="store_true",
                        help="Run only the owned Calculator arithmetic and visual selection checks")
    args = parser.parse_args()
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    selector = VisionSelector(args.model, port=args.port)
    report = {"stage": 10, "time": datetime.now(UTC).isoformat(),
              "screenshots_persisted": False, "microphone_tested": False,
              "scope": "Calculator only" if args.calculator_only else "Calculator and Notepad",
              "apps": []}
    began = time.perf_counter()
    report["vision_warm"] = {"ok": selector.warm(timeout_s=60.0),
                             "milliseconds": (time.perf_counter() - began) * 1000,
                             "outcome": selector.last_outcome}
    # Warmup is deliberately outside capture and the five-second selection timer.
    for app in (("Calculator",) if args.calculator_only else ("Calculator", "Notepad")):
        report["apps"].append(probe_app(app, application, selector))
    report["verified"] = (report["vision_warm"]["ok"]
                          and all(row["verified"] for row in report["apps"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
