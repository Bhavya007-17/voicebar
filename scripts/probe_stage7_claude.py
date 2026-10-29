"""Claude verification explicitly authorized by the user for its existing window."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from voicebar.executor import Context, execute
from voicebar.executor.uia import Automation, ControlTreeChanged
from voicebar.parser import parse

ROOT = Path(__file__).resolve().parents[1]


def relevant(rows):
    names = {"New", "Write your prompt to Claude", "Dictate", "Voice input", "Stop dictation",
             "Stop", "Cancel", "Send message", "Stop recording", "Start recording", "New session - Claude",
             "Finish dictation", "Cancel dictation"}
    return [row for row in rows if row["name"] in names or row["role"] in {"edit", "dialog"}]


def main():
    report = {"explicit_user_authorization": True, "actions": []}
    with Context(ROOT, owned={}, persist=False) as ctx:
        windows = ctx.apps.find("Claude")
        if len(windows) != 1:
            raise RuntimeError("Claude window missing or ambiguous")
        ctx.adopt(windows[0])  # User explicitly waived the owned-window rule for Claude.
        report["window"] = asdict(windows[0])
        automation = Automation()
        try:
            for phrase in ("new chat in claude", "focus the message box in claude",
                           "start voice input in claude"):
                plan = parse(phrase, ctx.loaded.catalog)
                item = {"phrase": phrase, "plan": plan, "results": execute(plan, context=ctx)}
                report["actions"].append(item)
                def ready_tree():
                    try:
                        return automation.snapshot(windows[0].hwnd)
                    except ControlTreeChanged:
                        return False

                rows = ctx.native.wait(ready_tree, 5, "Claude's current accessible tree")
                item["controls"] = relevant(rows)
                item["focused"] = automation.focused()
                # Preserve full local inspection without copying personal chat titles into docs.
                (ROOT / ".cache/stage7-claude-after.json").write_text(
                    json.dumps(rows, indent=2), encoding="utf-8")
                print(json.dumps(item, ensure_ascii=True), flush=True)
                if not all(result["ok"] for result in item["results"]):
                    break
        finally:
            automation.close()
            (ROOT / "docs/evidence/stage7-claude-verified.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
