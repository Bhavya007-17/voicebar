"""Read-only observer of one actual GUI microphone decision and Claude state."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from voicebar.executor import Context
from voicebar.executor.uia import Automation

ROOT = Path(__file__).resolve().parents[1]
CONTROL_NAMES = {"Dictate", "Finish dictation", "Cancel dictation", "Write your prompt to Claude"}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("log", type=Path)
    args.add_argument("--output", type=Path, required=True)
    args.add_argument("--latest", action="store_true", help="Inspect the latest recorded decision")
    options = args.parse_args()
    baseline = 0 if options.latest else len(options.log.read_text(encoding="utf-8-sig").splitlines())
    report = {"source_log": str(options.log), "read_only_observer": True,
              "physical_microphone_verified": False, "handoff_verified": False}
    deadline = time.monotonic() + 180
    print("Waiting for the next actual microphone decision; observer sends no input.", flush=True)
    while time.monotonic() < deadline:
        rows = options.log.read_text(encoding="utf-8-sig").splitlines()[baseline:]
        if options.latest:
            rows.reverse()
        for line in rows:
            try:
                decision = json.loads(line)
            except json.JSONDecodeError:
                continue  # a producer can still be finishing the current line
            if decision.get("event") != "decision":
                continue
            report["decision"] = decision
            report["physical_microphone_verified"] = True
            with Context(ROOT, owned={}, persist=False) as ctx:
                windows = ctx.apps.find("Claude")
                if len(windows) != 1:
                    report["state_error"] = "Claude window missing or ambiguous"
                else:
                    automation = Automation()
                    try:
                        controls = automation.find(windows[0].hwnd, {"role": "button"})
                        report["state_controls"] = [row for row in controls
                                                    if row["name"] in CONTROL_NAMES]
                    finally:
                        automation.close()
            report["handoff_verified"] = bool(
                decision.get("state_after") == "asleep"
                and decision.get("post_action_mode") == "sleep"
                and any(action.get("action") == "app_action"
                        and action.get("app") == "Claude"
                        and action.get("name") == "start_voice_input"
                        for action in decision.get("plan") or [])
                and decision.get("results")
                and all(result["ok"] for result in decision["results"])
                and any(row["name"] == "Finish dictation"
                        for row in report.get("state_controls", []))
            )
            options.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report), flush=True)
            return
        threading.Event().wait(.2)  # poll an observable state, without altering an action timeout
    report["state_error"] = "No new microphone decision within the observer window"
    options.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
