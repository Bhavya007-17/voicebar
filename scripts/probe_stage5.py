"""Exercise the real desktop/model/microphone path with all actions in dry run.

This finite probe starts through CLI app, waits for two seconds of real callback
samples, switches Off, confirms native closure, and quits through the desktop
controller. It never opens or drives a target application. Hardware hotplug,
sleep/resume and the visible Windows microphone indicator remain human checks.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

from voicebar.stt.offline import NativeEndpointMonitor, SocketAuditPolicy


def main():
    root = Path(__file__).resolve().parents[1]
    policy = SocketAuditPolicy()
    policy.install_and_check()
    from PySide6.QtCore import QTimer

    from voicebar.cli import main as cli_main
    from voicebar.ui import app as app_module
    from voicebar.ui.controller import SelectedInput

    report = {"stage": 5, "executor": "dry-run", "physical_network_disabled": False,
              "ready": False, "stream_opened": False, "stream_closed": False,
              "callback_samples": 0, "ui_ticks": 0, "max_ui_gap_ms": 0,
              "network_samples": 0, "network_endpoints": [], "failures": []}
    rows, sample_stop = [], threading.Event()
    began, last_tick = time.perf_counter(), [None]

    def monitor_network():
        try:
            monitor = NativeEndpointMonitor()
            while not sample_stop.is_set():
                endpoints = monitor.endpoints({os.getpid()})
                report["network_samples"] += 1
                if endpoints:
                    report["network_endpoints"].extend(asdict(item) for item in endpoints)
                sample_stop.wait(.05)
        except Exception as error:  # noqa: BLE001 - retain failed monitoring, never imply a pass
            report["failures"].append(f"Endpoint monitor: {error}")

    monitor = threading.Thread(target=monitor_network, daemon=True)
    monitor.start()

    class CountingInput(SelectedInput):
        def open(self, callback):
            def receive(pcm, frames, status):
                if not status:
                    report["callback_samples"] += frames
                callback(pcm, frames, status)
            return super().open(receive)

    class ObservedDesktop(app_module.DesktopApp):
        def start(self):
            self.controller.session.dry_run = True
            self.controller._microphone_factory = CountingInput
            original = self.controller._log

            def record(row):
                original(row)
                rows.append(dict(row))
            record.path = original.path
            self.controller._log = record
            original_emit = self.controller._emit

            def emit(**row):
                if row.get("event") == "error":
                    report["failures"].append(row.get("detail", "Unknown runtime error"))
                    print(json.dumps(row), flush=True)
                original_emit(**row)
            self.controller._emit = emit
            report["log"] = str(original.path)
            self.probe_phase = "loading"
            self.phase_at = time.perf_counter()
            self.probe_timer = QTimer(self.application)
            self.probe_timer.setInterval(25)
            self.probe_timer.timeout.connect(self.probe)
            super().start()
            self.probe_timer.start()

        def probe(self):
            now = time.perf_counter()
            report["ui_ticks"] += 1
            if last_tick[0] is not None:
                report["max_ui_gap_ms"] = max(report["max_ui_gap_ms"], (now - last_tick[0]) * 1000)
            last_tick[0] = now
            if report["failures"] and self.probe_phase != "quitting":
                self.quit()
                self.probe_phase, self.phase_at = "quitting", now
            if (now - began > 120 and self.probe_phase != "quitting"):
                report["failures"].append("Probe deadline expired")
                self.quit()
                self.probe_phase, self.phase_at = "quitting", now
            if self.probe_phase == "loading" and self.controller.ready.is_set():
                report["ready"] = True
                report["hotkeys_registered"] = self.hotkeys.ready.is_set() and self.hotkeys.error is None
                self.controller.set_enabled(True)
                self.probe_phase = "capturing"
            if self.probe_phase == "capturing" and report["callback_samples"] >= 32000:
                self.controller.set_enabled(False)
                self.probe_phase = "closing"
            report["stream_opened"] = any(row.get("reason") == "stream_opened" for row in rows)
            report["stream_closed"] = any(row.get("reason") == "stream_closed"
                                          and row.get("stream_open") is False for row in rows)
            if self.probe_phase == "closing" and report["stream_closed"]:
                self.bar.set_feedback({"enabled": False, "stream_open": False, "state": "off",
                                       "text": "", "detail": "Ready · microphone closed"})
                self.bar.expand()
                self.probe_phase = "screenshot"
            if self.probe_phase == "screenshot" and self.bar.height() == self.bar.expanded_height:
                target = root / "docs/evidence/stage5-bar.png"
                report["screenshot_saved"] = self.bar.grab().save(str(target))
                self.quit()
                self.probe_phase, self.phase_at = "quitting", now
            if self.probe_phase == "quitting" and now - self.phase_at > 10:
                report["failures"].append("Desktop quit cleanup deadline expired")
                self.application.exit(1)

    app_module.DesktopApp = ObservedDesktop
    try:
        report["returncode"] = cli_main(["app"], project_root=root)
    finally:
        sample_stop.set()
        monitor.join(1)
    report["seconds"] = time.perf_counter() - began
    report["runtime_socket_attempts"] = [row for row in policy.events if row["phase"] == "runtime"]
    report["runtime_stopped"] = next((row for row in reversed(rows) if row.get("event") == "runtime_stopped"), None)
    report["unverified"] = ["visible Windows microphone indicator", "monitor hotplug", "mixed physical scaling",
                            "sleep/resume", "exclusive full screen", "voice-only session", "login startup"]
    report["ok"] = (report["returncode"] == 0 and report["ready"] and report["stream_opened"]
                    and report["stream_closed"] and report["callback_samples"] >= 32000
                    and not report["runtime_socket_attempts"] and not report["network_endpoints"]
                    and report["network_samples"] > 0 and not report["failures"]
                    and bool(report["runtime_stopped"])
                    and not report["runtime_stopped"]["workers_alive"])
    path = root / "docs/evidence/stage5-app-smoke.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
