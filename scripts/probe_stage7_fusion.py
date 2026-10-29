"""Run only fixed Fusion actions in the Fusion process opened by Stage 7."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

from voicebar import fusion_mcp
from voicebar.executor import Context, execute
from voicebar.executor.uia import Automation
from voicebar.parser import parse

ROOT = Path(__file__).resolve().parents[1]


def main(phrases=("new design in fusion", "create sketch in fusion", "fit view in fusion"),
         evidence="stage7-fusion-verified.json"):
    saved = ROOT / ".cache/stage7-ownership.json"
    ownership = json.loads(saved.read_text(encoding="utf-8"))
    launch = json.loads((ROOT / "docs/evidence/stage7-app-probe.json").read_text(encoding="utf-8"))
    fusion = [item for item in launch["apps"] if item["app"] == "Autodesk Fusion"]
    if len(fusion) != 1 or not fusion[0].get("launch", [{}])[0].get("ok"):
        raise RuntimeError("No successful owned Fusion launch record")
    pid = fusion[0]["window"]["pid"]
    previous_path = ROOT / "docs/evidence/stage7-fusion-verified.json"
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("owned_process") in ownership["owned"].values():
            pid = previous["owned_process"]
        archive = ROOT / "docs/evidence/stage7-fusion-attempt1.json"
        if not archive.exists():
            archive.write_text(previous_path.read_text(encoding="utf-8"), encoding="utf-8")
    report = {"runtime_deadline_seconds": 3, "actions": [], "owned_process": pid}
    with Context(ROOT, owned={int(hwnd): value for hwnd, value in ownership["owned"].items()},
                 persist=False, cancel=threading.Event()) as ctx:
        # Fusion replaces its owned splash HWND with a main HWND in the same
        # process. No pre-existing Fusion process was adopted by the launcher.
        existing = ctx.apps.find("Autodesk Fusion")
        if not existing:
            report["relaunch"] = execute([{"action": "open_app", "app": "Autodesk Fusion"}],
                                         context=ctx)
            if not report["relaunch"][0]["ok"]:
                raise RuntimeError("Owned Fusion relaunch failed")
            splash = ctx.target("Autodesk Fusion")
            pid = splash.pid
            report["owned_process"] = pid
            windows = ctx.wait(lambda: [window for window in ctx.apps.find("Autodesk Fusion")
                                       if window.pid == pid and "Home - Autodesk Fusion" in window.title],
                               "owned Fusion main window")
        else:
            windows = [window for window in existing if window.pid == pid]
        if len(windows) != 1:
            raise RuntimeError("Owned Fusion main window missing or ambiguous")
        ctx.adopt(windows[0])
        automation = Automation()
        original = fusion_mcp.call_command
        verified = []

        def recorded(command, *, guard, cancel=None):
            result = original(command, guard=guard, cancel=cancel)
            verified.append(result)
            return result

        fusion_mcp.call_command = recorded
        try:
            for phrase in phrases:
                plan = parse(phrase, ctx.loaded.catalog)
                item = {"phrase": phrase, "plan": plan}
                report["actions"].append(item)
                count = len(verified)
                item["results"] = execute(plan, context=ctx)
                item["verified_state"] = verified[count:]
                item["window"] = asdict(ctx.target("Autodesk Fusion"))
                try:
                    item["controls"] = automation.snapshot(windows[0].hwnd)
                except Exception as error:  # noqa: BLE001 - preserve independent UIA failure
                    item["uia_error"] = str(error)
                print(json.dumps({key: value for key, value in item.items() if key != "controls"}),
                      flush=True)
                if not all(result["ok"] for result in item["results"]):
                    break  # Writes are never retried after an uncertain result.
        finally:
            fusion_mcp.call_command = original
            automation.close()
            (ROOT / "docs/evidence" / evidence).write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            ownership["owned"].update({str(hwnd): value for hwnd, value in ctx.owned.items()})
            saved.write_text(json.dumps(ownership), encoding="utf-8")


if __name__ == "__main__":
    main()
