"""Observe an owned fresh-clone CLI app until Ready/Off, then quit it cleanly."""

import argparse
import json
import time
from pathlib import Path

from PySide6.QtCore import QTimer

import voicebar
from voicebar import cli
from voicebar.ui import app as desktop_module


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--root", type=Path, required=True)
    arguments.add_argument("--output", type=Path, required=True)
    args = arguments.parse_args()
    root = args.root.resolve()
    if Path(voicebar.__file__).resolve().parent.parent != root:
        raise RuntimeError("The probe must import the fresh clone's installed package")
    original = desktop_module.DesktopApp
    result = {"root": str(root), "package": voicebar.__file__, "scope":
              "Instrumented voicebar app CLI; real tray/bar, model and global keys; no microphone or actions",
              "ready": False, "errors": []}
    started = time.monotonic()

    class ObservedDesktop(original):
        def __init__(self, *values, **options):
            super().__init__(*values, **options)
            self.probe_timer = QTimer(self.application)
            self.probe_timer.setInterval(25)
            self.probe_timer.timeout.connect(self.observe)
            self.probe_timer.start()

        def observe(self):
            if self.controller.ready.is_set() and not self._quitting:
                result.update(ready=True, stream_open=self.controller._stream_open,
                              enabled=self.controller._enabled, bar_visible=self.bar.isVisible(),
                              hotkeys_error=self.hotkeys.error,
                              copilot_error=self.hotkeys.copilot_error,
                              ready_seconds=time.monotonic() - started)
                self.quit()
            elif time.monotonic() - started > 120 and not self._quitting:
                result["errors"].append("Model-ready deadline expired")
                self.quit()
            if self.controller.closed.is_set():
                result.update(closed=True, stream_closed=not self.controller._stream_open,
                              hotkeys_stopped=self.hotkeys.stopped.is_set())
                self.probe_timer.stop()

    desktop_module.DesktopApp = ObservedDesktop
    try:
        result["exit_code"] = cli.main(["app"], project_root=root)
    finally:
        desktop_module.DesktopApp = original
        result["ok"] = bool(result["ready"] and result.get("bar_visible")
                            and not result.get("enabled") and not result.get("stream_open")
                            and result.get("closed") and result.get("stream_closed")
                            and result.get("hotkeys_stopped") and not result.get("hotkeys_error")
                            and not result.get("copilot_error") and not result["errors"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
