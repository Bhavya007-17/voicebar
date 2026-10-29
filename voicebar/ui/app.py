"""Tray and bar composition. All audio/model/action work stays in the controller."""

from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .bar import COLORS, TopBar
from .native import HotkeyManager, SingleInstance, StartupShortcut
from .screen_capture import ScreenCapture
from .settings import load_ui_settings


def status_icon(feedback):
    color = (COLORS["off"] if not feedback.get("stream_open") else
             COLORS["dictation"] if feedback.get("dictating") or feedback.get("state") == "dictation" else
             "#ff6868" if feedback.get("state") in {"off", "locked"} else
             "#ffbb66" if feedback.get("mode") == "open" else
             COLORS.get(feedback.get("state"), COLORS["idle"]))
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(11, 3, 10, 18, 5, 5)
    from PySide6.QtGui import QPen
    painter.setPen(QPen(QColor(color), 3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(6, 9, 20, 17, 180 * 16, 180 * 16)
    painter.drawLine(16, 26, 16, 29)
    painter.drawLine(11, 29, 21, 29)
    painter.end()
    return QIcon(pixmap)


class DesktopApp:
    def __init__(self, application, root, loaded, *, controller=None, settings=None):
        from .controller import RuntimeController
        self.application, self.root = application, Path(root)
        self.settings = settings or load_ui_settings(self.root)
        self.controller = controller or RuntimeController(
            self.root, loaded, execution_timeout=self.settings["execution_timeout_seconds"])
        self.screen_capture = ScreenCapture(self.controller, application)
        self.bar = TopBar(self.settings)
        self.hotkey_events = queue.Queue()
        self.hotkeys = HotkeyManager(self.hotkey_events, copilot_key=self.settings.get("copilot_key", True), **{
            key: self.settings[key] for key in ("toggle_listening", "toggle_bar", "hold_to_talk")})
        self.startup = StartupShortcut(self.root)
        self.jobs = queue.Queue()
        self._quitting = False
        self._startup_busy = False
        self._microphone_index = None
        self.instance = None
        self._hotkey_reload = None
        self.tray = QSystemTrayIcon(status_icon(self.bar.feedback), application)
        self.menu = QMenu()
        self.listen = self.menu.addAction("Listening")
        self.listen.setCheckable(True)
        self.listen.triggered.connect(self.controller.set_enabled)
        self.open_mode = self.menu.addAction("Open mode (pinned apps and safe commands)")
        self.open_mode.setCheckable(True)
        self.open_mode.triggered.connect(lambda value: self.controller.set_mode("open" if value else "name"))
        self.microphones = self.menu.addMenu("Microphone")
        self.microphones.aboutToShow.connect(self.controller.refresh_microphones)
        self.microphones.addAction("Finding microphones…").setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Show / hide bar", self.bar.toggle)
        self.menu.addAction("Open config folder", lambda: self._open_folder("config"))
        self.menu.addAction("Open log folder", lambda: self._open_folder("logs"))
        self.startup_action = self.menu.addAction("Start with Windows")
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(self.startup.enabled)
        self.startup_action.triggered.connect(self._startup_changed)
        self.menu.addSeparator()
        self.menu.addAction("Quit Voicebar", self.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.bar.listening_changed.connect(self.controller.set_enabled)
        self.bar.mode_changed.connect(self.controller.set_mode)
        self.bar.system_event.connect(self._system_event)
        self.timer = QTimer(application)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def start(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("Windows notification area is unavailable; Voicebar cannot start")
        self.tray.show()
        self.bar.show()
        self.bar.expand()
        self.hotkeys.start()
        self.controller.start()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.bar.toggle()

    def _system_event(self, event):
        if event == "suspend":
            self.controller.suspend()
        elif event == "resume":
            self.controller.resume()
        elif event == "locked":
            self.controller.suspend()
        elif event == "unlocked":
            self.controller.resume()
        if event in {"resume", "display_changed", "unlocked"}:
            self.bar.reposition()

    def _background(self, function):
        def run():
            try:
                function()
                self.jobs.put({"event": "job_done"})
            except Exception as error:  # noqa: BLE001 - transfer settings worker failures to the bar
                self.jobs.put({"event": "error", "detail": str(error)})
        threading.Thread(target=run, name="bar-settings", daemon=True).start()

    def _open_folder(self, name):
        def open_folder():
            directory = self.root / name
            directory.mkdir(exist_ok=True)
            os.startfile(directory)
        self._background(open_folder)

    def _startup_changed(self, enabled):
        if self._startup_busy:
            return
        self._startup_busy = True
        self.startup_action.setEnabled(False)
        self._background(lambda: self.startup.set_enabled(enabled))

    def _microphone_menu(self, devices):
        self.microphones.clear()
        group = QActionGroup(self.microphones)
        group.setExclusive(True)
        for index, name in [(None, "System default"), *[
                (row["index"], row["name"]) for row in devices]]:
            action = self.microphones.addAction(name)
            action.setCheckable(True)
            action.setData(index)
            action.setChecked(index == self._microphone_index)
            action.triggered.connect(lambda checked=False, selected=index: self.controller.set_microphone(selected))
            group.addAction(action)
        self._microphone_group = group

    def poll(self):
        if self.instance is not None and self.instance.activation_requested():
            self.bar.show()
            self.bar.expand()
        for source in (self.controller.events, self.jobs):
            for _ in range(128):
                try:
                    row = source.get_nowait()
                except queue.Empty:
                    break
                if row.get("event") == "microphones":
                    self._microphone_menu(row.get("devices", []))
                if row.get("event") == "config_reloaded":
                    settings = row["settings"]
                    self.bar.settings.update(settings)
                    self.bar.reposition()
                    keys = ("toggle_listening", "toggle_bar", "hold_to_talk", "copilot_key")
                    if any(settings.get(key) != self.settings.get(key) for key in keys):
                        self.hotkeys.close()
                        self._hotkey_reload = settings
                    self.settings = settings
                if "microphone" in row:
                    self._microphone_index = row["microphone"]
                    if hasattr(self, "_microphone_group"):
                        for action in self._microphone_group.actions():
                            action.setChecked(action.data() == self._microphone_index)
                if source is self.jobs and self._startup_busy:
                    self._startup_busy = False
                    self.startup_action.setEnabled(True)
                    self.startup_action.setChecked(self.startup.enabled)
                self.bar.set_feedback(row)
                self.listen.setChecked(bool(self.bar.feedback["enabled"]))
                self.open_mode.setChecked(self.bar.feedback["mode"] == "open")
                self.tray.setIcon(status_icon(self.bar.feedback))
                self.tray.setToolTip("Voicebar · " + self.bar.status.text())
        for _ in range(32):
            try:
                command = self.hotkey_events.get_nowait()
            except queue.Empty:
                break
            if command == "toggle_bar":
                self.bar.toggle()
            elif command == "toggle_listening":
                self.controller.toggle_enabled()
            elif command in {"hold_start", "hold_end"}:
                getattr(self.controller, command)()
            elif command == "hotkey_error":
                self.bar.set_feedback({"event": "error", "detail": self.hotkeys.error})
            elif command == "copilot_error":
                self.bar.set_feedback({"event": "error", "detail": self.hotkeys.copilot_error})
        if self._hotkey_reload is not None and self.hotkeys.stopped.is_set() and not self._quitting:
            settings, self._hotkey_reload = self._hotkey_reload, None
            while not self.hotkey_events.empty():
                self.hotkey_events.get_nowait()
            self.hotkeys = HotkeyManager(self.hotkey_events, **{
                key: settings[key] for key in ("toggle_listening", "toggle_bar", "hold_to_talk", "copilot_key")})
            self.hotkeys.start()
        if self._quitting and self.controller.closed.is_set() and self.hotkeys.stopped.is_set():
            self.timer.stop()
            self.tray.hide()
            self.bar.close()
            self.application.quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.listen.setEnabled(False)
        self.bar.set_feedback({"event": "state", "detail": "Closing microphone and local workers…"})
        self.controller.close()
        self.hotkeys.close()


def run_app(root, loaded):
    if sys.platform != "win32":
        raise RuntimeError("Voicebar app requires native Windows")
    application = QApplication.instance() or QApplication([])
    application.setApplicationName("Voicebar")
    application.setQuitOnLastWindowClosed(False)
    instance = SingleInstance()
    try:
        desktop = DesktopApp(application, root, loaded)
        desktop.instance = instance
        desktop.start()
        return application.exec()
    finally:
        instance.close()
