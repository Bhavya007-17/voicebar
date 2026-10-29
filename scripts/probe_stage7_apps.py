"""Open separate Stage 7 test apps; retain ownership for subsequent probes."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from voicebar.executor import Context, execute
from voicebar.executor.uia import Automation

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence"


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report = {"stage": 7, "apps": [], "existing_apps_read_only": []}
    with Context(ROOT, owned={}, persist=False) as ctx:
        ctx.timeout = 90
        baseline = ctx.native.windows()
        automation = Automation(timeout=15)
        try:
            for window in baseline:
                if ctx.apps.matches(window, "Claude"):
                    item = {"window": asdict(window), "read_only": True}
                    try:
                        item["controls"] = automation.snapshot(window.hwnd)
                    except Exception as error:  # noqa: BLE001 - preserve failed probe evidence
                        item["error"] = str(error)
                    report["existing_apps_read_only"].append(item)
                    print(json.dumps({"app": "Claude", "read_only": True,
                                      "controls": len(item.get("controls", [])),
                                      "error": item.get("error")}), flush=True)
            for app in ("Google Chrome", "Word", "Autodesk Fusion"):
                item = {"app": app}
                report["apps"].append(item)
                existing = ctx.apps.find(app)
                if existing:
                    item["blocked"] = "Existing unowned app: launch refused"
                    continue
                item["launch"] = execute([{"action": "open_app", "app": app}], context=ctx)
                if item["launch"][0]["ok"]:
                    window = ctx.target(app)
                    item["window"] = asdict(window)
                    try:
                        item["controls"] = automation.snapshot(window.hwnd)
                    except Exception as error:  # noqa: BLE001 - preserve failed probe evidence
                        item["error"] = str(error)
                print(json.dumps({"app": app, "launch": item["launch"],
                                  "controls": len(item.get("controls", [])),
                                  "error": item.get("error")}), flush=True)
                (EVIDENCE / "stage7-app-probe.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        finally:
            automation.close()
            ownership = {"owned": {str(hwnd): pid for hwnd, pid in ctx.owned.items()}}
            (ROOT / ".cache/stage7-ownership.json").write_text(json.dumps(ownership), encoding="utf-8")
            (EVIDENCE / "stage7-app-probe.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
