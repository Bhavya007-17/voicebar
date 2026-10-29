"""Verify exact generic clicking in a Calculator opened by this probe."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from voicebar.executor import Context, execute
from voicebar.executor.uia import Automation
from voicebar.parser import parse

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {}
    with Context(ROOT, owned={}, persist=False) as ctx:
        if ctx.apps.find("Calculator"):
            raise RuntimeError("Existing unowned Calculator; probe refused")
        report["launch"] = execute([{"action": "open_app", "app": "Calculator"}], context=ctx)
        if not report["launch"][0]["ok"]:
            raise RuntimeError(str(report["launch"]))
        window = ctx.target("Calculator")
        report["window"] = asdict(window)
        automation = Automation()
        try:
            report["before"] = automation.snapshot(window.hwnd)
            report["plan"] = parse("click one", ctx.loaded.catalog)
            report["result"] = execute(report["plan"], context=ctx)
            report["after"] = automation.snapshot(window.hwnd)
            report["no_match"] = execute(parse("click voicebar nonexistent control", ctx.loaded.catalog),
                                         context=ctx)
            report["after_no_match"] = automation.snapshot(window.hwnd)
            print(json.dumps({key: value for key, value in report.items()
                              if key not in {"before", "after", "after_no_match"}}), flush=True)
        finally:
            automation.close()
            report["cleanup"] = execute([{"action": "close_app", "app": "Calculator"}], context=ctx)
            (ROOT / "docs/evidence/stage7-click-verified.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
