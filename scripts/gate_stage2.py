"""Watched CLI gate, native state queries and measured action-start latency.

Run from project root. Every action is restricted by a persisted HWND/PID set.
Existing user browser/Fusion/Claude windows are recorded and never adopted.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicebar.executor import Context, execute


def main():
    session = ROOT / ".cache/stage2-gate-session.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(json.dumps({"owned": {}, "last_app": None}), encoding="utf-8")
    previous_session = os.environ.get("VOICEBAR_TEST_SESSION")
    os.environ["VOICEBAR_TEST_SESSION"] = str(session)
    evidence = {"steps": [], "latency": {}, "scope": "new HWND/PID only"}
    ctx = Context(ROOT)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    evidence["baseline_windows"] = [asdict(window) for window in ctx.native.windows()]
    blank_verified = set()

    def reload_scope():
        data = json.loads(session.read_text(encoding="utf-8"))
        ctx.owned = {int(hwnd): int(pid) for hwnd, pid in data["owned"].items()}
        ctx.last_app = data.get("last_app")

    def cli(text, verify):
        command = ["uv", "run", "voicebar", "run", text]
        print("PS> " + subprocess.list2cmdline(command), flush=True)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=90, shell=False, check=False)
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="", flush=True)
        step = {"command": command, "stdout": result.stdout, "stderr": result.stderr,
                "returncode": result.returncode}
        evidence["steps"].append(step)
        reload_scope()
        assert result.returncode == 0, step
        step["state"] = verify()
        print("STATE " + json.dumps(step["state"], ensure_ascii=True), flush=True)

    def state(app):
        window = ctx.target(app)
        return {**asdict(window), "foreground": ctx.native.foreground() == window.hwnd,
                "placement": ctx.native.placement(window.hwnd),
                "monitor": ctx.native.monitor(window.hwnd), "rect": ctx.native.rect(window.hwnd)}

    def show(app, expected):
        observed = state(app)
        assert observed["placement"]["show"] == expected, observed
        return observed

    def typed():
        window = ctx.target("Notepad")
        expected = "Hello, World! 123\n"
        def content():
            text = ctx.native.read_text(window.hwnd).replace("\r\n", "\n").replace("\r", "\n")
            return text if text == expected else None
        text = ctx.wait(content, "exact Notepad text and Enter")
        return {"hwnd": window.hwnd, "text": text}

    def gone(app):
        assert not ctx.apps.find(app, ctx.owned)
        return {"app": app, "owned_windows": 0}

    try:
        cli("open notepad", lambda: state("Notepad"))
        notepad = ctx.target("Notepad")
        assert (notepad.hwnd, notepad.pid) not in baseline
        def blank_editor():
            try:
                return (True, ctx.native.read_text(notepad.hwnd))
            except RuntimeError as error:
                if "no readable standard edit control" in str(error):
                    return None
                raise
        _, original = ctx.wait(blank_editor, "owned Notepad editor to be readable")
        assert original == "", "Restored Notepad text left untouched"
        blank_verified.add(notepad.hwnd)
        cli("make it full screen", lambda: show("Notepad", 3))
        if len(ctx.native.monitors()) > 1:
            before = ctx.native.monitor(notepad.hwnd)
            def moved():
                observed = show("Notepad", 3)
                assert observed["monitor"] != before
                return observed
            cli("move it to the other screen", moved)
        else:
            print("SKIP move it to the other screen: one connected monitor", flush=True)
            evidence["monitor_skip"] = "One connected monitor"
        cli("type Hello, World! 123 and press enter", typed)
        cli("snap it to the left", lambda: state("Notepad"))
        cli("open calculator and maximize it", lambda: show("Calculator", 3))
        cli("close calculator", lambda: gone("Calculator"))
        cli("open youtube", lambda: state("active"))
        browser = ctx.target()
        ctx.wait(lambda: any("youtube" in window.title.casefold() for window in ctx.native.windows()
                             if window.hwnd == browser.hwnd), "YouTube title or host")
        cli("close youtube", lambda: {"browser_window_closed":
                                       not ctx.native.is_window(browser.hwnd, browser.pid)})
        assert not ctx.native.is_window(browser.hwnd, browser.pid)

        samples = {"window": [], "key": [], "tab": []}
        def sample(family, action):
            result = execute([action], context=ctx)[0]
            assert result["ok"] and result["started_ms"] is not None, result
            samples[family].append(result)
        ctx.guard(notepad)
        ctx.native.activate(notepad.hwnd, ctx.timeout)
        for _ in range(8):
            for op in ("maximize", "restore"):
                sample("window", {"action": "window", "op": op, "target": "Notepad"})
            sample("key", {"action": "press", "keys": "ctrl+home"})
        result = execute([{"action": "open_url", "url": ctx.loaded.catalog.sites["youtube"]}], context=ctx)
        assert result[0]["ok"], result
        browser = ctx.target()
        ctx.wait(lambda: any("youtube" in window.title.casefold() for window in ctx.native.windows()
                             if window.hwnd == browser.hwnd), "YouTube title or host")
        for _ in range(8):
            previous = ctx.target().title
            sample("tab", {"action": "tab", "op": "new"})
            ctx.wait(lambda title=previous: ctx.target().title != title, "new tab title")
            sample("tab", {"action": "tab", "op": "close"})
        for family, rows in samples.items():
            times = sorted(row["started_ms"] for row in rows)
            evidence["latency"][family] = {"samples": len(times),
                "median_started_ms": statistics.median(times),
                "p95_started_ms": times[max(0, __import__("math").ceil(len(times) * .95) - 1)],
                "results": rows}
            assert statistics.median(times) < 100, (family, times)
            print(f"LATENCY {family}: median={statistics.median(times):.4f} ms, n={len(times)}", flush=True)
        evidence["passed"] = True
    finally:
        cleanup = []
        failures = []
        try:
            reload_scope()
            for window in ctx.native.windows():
                if ctx.owned.get(window.hwnd) != window.pid:
                    continue
                try:
                    ctx.guard(window)
                    if ctx.apps.matches(window, "Notepad"):
                        if window.hwnd not in blank_verified:
                            raise RuntimeError("Initial text was not proven blank; left untouched")
                        ctx.native.clear_text(window.hwnd)
                    ctx.native.close(window.hwnd)
                    ctx.wait(lambda target=window: not ctx.native.is_window(target.hwnd, target.pid), "owned gate cleanup")
                    cleanup.append(window.hwnd)
                except (RuntimeError, OSError) as error:
                    failures.append(f"{window.hwnd}/{window.pid}: {error}")
            evidence["baseline_windows_still_present"] = all(ctx.native.is_window(hwnd, pid)
                                                            for hwnd, pid in baseline)
        finally:
            evidence["cleanup_closed"] = cleanup
            evidence["cleanup_failures"] = failures
            if failures:
                evidence["passed"] = False
            destination = ROOT / "docs/evidence/stage2-live-gate.json"
            destination.write_text(json.dumps(evidence, ensure_ascii=True, indent=2), encoding="utf-8")
            try:
                ctx.close()
            finally:
                if previous_session is None:
                    os.environ.pop("VOICEBAR_TEST_SESSION", None)
                else:
                    os.environ["VOICEBAR_TEST_SESSION"] = previous_session
            print("Evidence: " + str(destination), flush=True)
        if failures:
            raise RuntimeError("Owned gate cleanup failures: " + "; ".join(failures))


if __name__ == "__main__":
    main()
