"""Tray controls share the runtime queue and quit without GUI-thread joins."""

import queue
import threading
from types import SimpleNamespace

from voicebar.ui.app import DesktopApp


def test_tray_controls_microphone_feedback_and_clean_quit(qapp, tmp_path, monkeypatch):
    calls = []
    controller = SimpleNamespace(events=queue.Queue(), closed=threading.Event(),
                                 set_enabled=lambda enabled: calls.append(("enabled", enabled)),
                                 set_mode=lambda mode: calls.append(("mode", mode)),
                                 set_microphone=lambda index: calls.append(("microphone", index)),
                                 refresh_microphones=lambda: calls.append(("refresh_microphones",)),
                                 close=lambda: calls.append(("close",)),
                                 suspend=lambda: calls.append(("suspend",)),
                                 resume=lambda: calls.append(("resume",)))
    hotkeys = SimpleNamespace(stopped=threading.Event(), close=lambda: calls.append(("hotkey_close",)))
    monkeypatch.setattr("voicebar.ui.app.HotkeyManager", lambda *args, **kwargs: hotkeys)
    monkeypatch.setattr("voicebar.ui.app.StartupShortcut", lambda *args: SimpleNamespace(enabled=False))
    monkeypatch.setattr(qapp, "quit", lambda: calls.append(("quit",)))
    settings = {"monitor": "primary", "dwell_ms": 450, "idle_ms": 4000,
                "toggle_listening": "ctrl+alt+f9", "toggle_bar": "ctrl+alt+f10",
                "hold_to_talk": "ctrl+alt+f8", "execution_timeout_seconds": 10}
    desktop = DesktopApp(qapp, tmp_path, None, controller=controller, settings=settings)
    desktop.timer.stop()
    try:
        desktop.listen.trigger()
        desktop.open_mode.trigger()
        assert calls[:2] == [("enabled", True), ("mode", "open")]
        desktop.microphones.aboutToShow.emit()
        assert calls[-1] == ("refresh_microphones",)
        controller.events.put({"event": "microphones", "devices": [{"index": 5, "name": "Owned fake mic"}]})
        controller.events.put({"event": "control", "enabled": True, "mode": "open", "microphone": 5})
        desktop.poll()
        selected = desktop._microphone_group.checkedAction()
        assert selected.text() == "Owned fake mic" and selected.data() == 5
        desktop._system_event("suspend")
        desktop._system_event("resume")
        assert calls[-2:] == [("suspend",), ("resume",)]
        desktop.quit()
        assert calls[-2:] == [("close",), ("hotkey_close",)]
        desktop.poll()
        assert ("quit",) not in calls
        controller.closed.set()
        hotkeys.stopped.set()
        desktop.poll()
        assert calls[-1] == ("quit",)
    finally:
        desktop.timer.stop()
        desktop.bar.close()
        desktop.bar.deleteLater()
        desktop.menu.deleteLater()
        desktop.tray.deleteLater()
        qapp.processEvents()
