"""Verify the configured gate-to-Claude listening handoff; no microphone claim."""
from __future__ import annotations

import json
import time
from pathlib import Path

from voicebar.executor import Context, execute
from voicebar.executor.app_actions import foreground_app
from voicebar.executor.apps import _activate
from voicebar.executor.input import _foreground_guard
from voicebar.executor.uia import Automation
from voicebar.listening import Chimes, GateSession, configured_gate

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {"input_kind": "text transcript", "physical_microphone_verified": False,
              "explicit_claude_window_authorization": True}
    with Context(ROOT, owned={}, persist=False) as ctx:
        windows = ctx.apps.find("Claude")
        if len(windows) != 1:
            raise RuntimeError("Claude window missing or ambiguous")
        window = windows[0]
        ctx.adopt(window)
        automation = Automation()
        try:
            before = automation.snapshot(window.hwnd)
            if any(row["name"] == "Finish dictation" for row in before):
                raise RuntimeError("Claude is already listening; existing session left untouched")
            session = GateSession(configured_gate(ROOT, ctx.loaded),
                                  execute=lambda plan: execute(plan, context=ctx),
                                  foreground_app=lambda: foreground_app(ROOT, ctx.loaded),
                                  chimes=Chimes(play=lambda: None))
            now = time.perf_counter()
            report["decision"] = session.hear("computer open claude and start voice mode",
                                               start=now - 2, end=now, speech_end=now,
                                               generation=session.generation)
            rows = automation.snapshot(window.hwnd)
            report["state_controls"] = [row for row in rows if row["name"] in {
                "Dictate", "Finish dictation", "Cancel dictation", "Write your prompt to Claude"}]
            report["state_after"] = session.state
            if (report["decision"]["reason"] != "run" or session.state != "asleep"
                    or not all(result["ok"] for result in report["decision"].get("results", []))
                    or not any(row["name"] == "Finish dictation" for row in rows)):
                raise RuntimeError("Claude listening handoff state was not verified")
            report["verified"] = True
            _activate(ctx, window)
            report["cleanup"] = automation.click(window.hwnd,
                                                  {"name": "Cancel dictation", "role": "button"},
                                                  lambda: _foreground_guard(ctx, window))
            print(json.dumps(report), flush=True)
        finally:
            automation.close()
            (ROOT / "docs/evidence/stage7-claude-chain.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
