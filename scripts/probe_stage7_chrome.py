"""Verify Chrome actions with isolated local tabs and owned HWND/PID guards."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from voicebar.cli import main as cli_main
from voicebar.executor import Context, execute
from voicebar.executor.apps import _activate
from voicebar.executor.uia import Automation
from voicebar.parser import parse

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {"actions": [], "local_pages_only": True, "accessibility_switch": True}
    work = ROOT / ".cache/stage7-chrome"
    work.mkdir(parents=True, exist_ok=True)
    pages = []
    for label in ("Alpha", "Beta"):
        page = work / f"{label}.html"
        page.write_text(f"<!doctype html><title>Voicebar {label}</title><h1>Voicebar {label}</h1>",
                        encoding="utf-8")
        pages.append(page.as_uri())
    saved = ROOT / ".cache/stage7-ownership.json"
    ownership = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {"owned": {}}
    with Context(ROOT, owned={int(hwnd): pid for hwnd, pid in ownership["owned"].items()}, persist=False) as ctx:
        baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
        # Executable path was observed in the app inventory, not guessed.
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        windows = ctx.apps.find("Google Chrome", ctx.owned)
        if not windows:
            process = subprocess.Popen([str(chrome), f"--user-data-dir={work / 'profile'}",
                                        "--no-first-run", "--no-default-browser-check",
                                        "--force-renderer-accessibility", *pages], shell=False)
            windows = ctx.wait(lambda: [window for window in ctx.apps.find("Google Chrome")
                                       if (window.hwnd, window.pid) not in baseline
                                       and window.pid == process.pid], "isolated Chrome window")
        if len(windows) != 1:
            raise RuntimeError("Ambiguous new isolated Chrome window")
        ctx.adopt(windows[0])
        _activate(ctx, windows[0])
        automation = Automation()
        try:
            def tree_ready():
                rows = automation.snapshot(ctx.target("Google Chrome").hwnd)
                return rows if any(row["name"] == "Voicebar Beta" for row in rows) else None
            initial = ctx.wait(tree_ready, "local tabs in Chrome accessibility tree")
            report["initial_controls"] = initial
            for phrase in ("switch to the tab named Voicebar Beta in chrome",
                           "search tabs in chrome", "new window in chrome",
                           "new incognito window in chrome"):
                window = ctx.target("Google Chrome")
                _activate(ctx, window)
                before = {(w.hwnd, w.pid) for w in ctx.native.windows()}
                plan = parse(phrase, ctx.loaded.catalog, active_app="Google Chrome")
                result = execute(plan, context=ctx)
                if all(row["ok"] for row in result) and "new " in phrase:
                    new = ctx.wait(lambda before=before: [w for w in ctx.apps.find("Google Chrome")
                                           if (w.hwnd, w.pid) not in before], "new Chrome action window")
                    if len(new) != 1:
                        raise RuntimeError("Ambiguous Chrome action result")
                    ctx.adopt(new[0])
                    window = new[0]
                rows = automation.snapshot(window.hwnd)
                current = [w for w in ctx.native.windows() if w.hwnd == window.hwnd]
                item = {"phrase": phrase, "plan": plan, "results": result,
                        "window_title": current[0].title if current else None, "controls": rows}
                report["actions"].append(item)
                print(json.dumps({k: v for k, v in item.items() if k != "controls"}), flush=True)
                if phrase.startswith("search"):
                    execute([{"action": "press", "keys": "esc"}], context=ctx)
            foreground = ctx.target("Google Chrome")
            _activate(ctx, foreground)
            report["inspect_exit"] = cli_main(["inspect", "--output",
                                              str(ROOT / "docs/evidence/stage7-inspect-chrome.json")])
            report["watch_exit"] = cli_main(["inspect", "--watch", "--duration", "0.5"])
        finally:
            automation.close()
            target = ROOT / "docs/evidence/stage7-chrome-verified.json"
            target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            saved = ROOT / ".cache/stage7-ownership.json"
            ownership = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {"owned": {}}
            ownership["owned"].update({str(hwnd): pid for hwnd, pid in ctx.owned.items()})
            saved.write_text(json.dumps(ownership), encoding="utf-8")


if __name__ == "__main__":
    main()
