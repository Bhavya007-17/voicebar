"""Compare read-only UIA trees on newly owned Notepad/Calculator windows.

Install candidates into .cache/stage7-candidates first; the project environment
and lockfile stay unchanged. Refuses to reuse any pre-existing app window.
"""

from __future__ import annotations

import importlib.metadata
import json
import site
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
site.addsitedir(str(ROOT / ".cache/stage7-candidates"))
sys.path.insert(0, str(ROOT))
sys.coinit_flags = 2

from voicebar.executor import Context, execute


def details(element):
    return {
        "name": element.CurrentName,
        "role": element.CurrentLocalizedControlType,
        "control_type": element.CurrentControlType,
        "automation_id": element.CurrentAutomationId,
        "keyboard_shortcut": element.CurrentAcceleratorKey or element.CurrentAccessKey,
        "enabled": bool(element.CurrentIsEnabled),
    }


def comtypes_tree(hwnd):
    import comtypes.client

    module = comtypes.client.GetModule("UIAutomationCore.dll")
    client = comtypes.client.CreateObject(module.CUIAutomation8, interface=module.IUIAutomation2)
    client.ConnectionTimeout = 1200
    client.TransactionTimeout = 1200
    root = client.ElementFromHandle(hwnd)
    elements = root.FindAll(module.TreeScope_Subtree, client.CreateTrueCondition())
    return [details(elements.GetElement(index)) for index in range(elements.Length)]


def pywinauto_tree(hwnd):
    from pywinauto import Desktop

    root = Desktop(backend="uia", allow_magic_lookup=False).window(handle=hwnd).wrapper_object()
    return [details(control.element_info.element) for control in [root, *root.descendants()]]


def uiautomation_tree(hwnd):
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    todo = [root]
    rows = []
    while todo:
        control = todo.pop(0)
        rows.append(details(control.Element))
        todo.extend(control.GetChildren())
    return rows


def win32_tree(ctx, hwnd):
    from voicebar.executor.native import WNDENUMPROC

    handles = []

    @WNDENUMPROC
    def callback(handle, _):
        handles.append(int(handle))
        return True

    ctx.native.user32.EnumChildWindows(hwnd, callback, 0)
    return [{"hwnd": handle} for handle in handles]


def main():
    report = {
        "libraries": {name: importlib.metadata.version(name)
                      for name in ("comtypes", "pywinauto", "uiautomation", "pywin32")},
        "sources": [
            "https://comtypes.readthedocs.io/en/stable/client.html",
            "https://comtypes.readthedocs.io/en/stable/com_interfaces.html",
            "https://pywinauto.readthedocs.io/en/latest/getting_started.html",
            "https://github.com/yinkaisheng/Python-UIAutomation-for-Windows/blob/master/readme.md",
        ],
        "sdk_header": "C:/Program Files (x86)/Windows Kits/10/Include/10.0.26100.0/um/UIAutomationClient.h",
        "observations": [],
    }
    ctx = Context(ROOT, owned={}, persist=False)
    baseline = {(window.hwnd, window.pid) for window in ctx.native.windows()}
    try:
        for app in ("Notepad", "Calculator"):
            item = {"app": app, "candidates": []}
            report["observations"].append(item)
            try:
                result = execute([{"action": "open_app", "app": app}], context=ctx)
                item["launch"] = result
                if not result[0]["ok"]:
                    continue
                window = ctx.target(app)
                assert (window.hwnd, window.pid) not in baseline
                assert ctx.owned.get(window.hwnd) == window.pid
                item["owned_window"] = {"hwnd": window.hwnd, "pid": window.pid, "title": window.title}
                for name, function in (("comtypes", comtypes_tree), ("pywinauto", pywinauto_tree),
                                       ("uiautomation", uiautomation_tree),
                                       ("ctypes_win32_children", lambda hwnd: win32_tree(ctx, hwnd))):
                    outcome = {"library": name}
                    started = perf_counter()
                    try:
                        rows = function(window.hwnd)
                        outcome.update(milliseconds=round((perf_counter() - started) * 1000, 3),
                                       count=len(rows), controls=rows)
                    except Exception as error:  # noqa: BLE001 - record candidate failures and clean up owned windows.
                        outcome.update(milliseconds=round((perf_counter() - started) * 1000, 3),
                                       error=f"{type(error).__name__}: {error}")
                    item["candidates"].append(outcome)
                    print(json.dumps({"app": app, **{key: value for key, value in outcome.items()
                                                   if key != "controls"}}), flush=True)
            except Exception as error:  # noqa: BLE001 - preserve launch diagnostics and cleanup evidence.
                item["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup = []
        for window in ctx.native.windows():
            if ctx.owned.get(window.hwnd) != window.pid:
                continue
            ctx.guard(window)
            # This trial never types or alters document contents.
            ctx.native.close(window.hwnd)
            ctx.wait(lambda target=window: not ctx.native.is_window(target.hwnd, target.pid),
                     "owned candidate window to close")
            cleanup.append({"hwnd": window.hwnd, "pid": window.pid, "closed": True})
        report["cleanup"] = cleanup
        ctx.close()
        target = ROOT / "docs/evidence/stage7-candidate-comparison.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(str(target), flush=True)


if __name__ == "__main__":
    main()
