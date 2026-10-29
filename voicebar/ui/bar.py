"""A nonactivating top edge tool window, using Qt logical screen coordinates."""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

COLORS = {"off": "#64748b", "locked": "#64748b", "idle": "#38d6ad",
          "listening": "#38d6ad", "hearing": "#64c8ff", "working": "#8e9cff",
          "done": "#38d6ad", "not sure": "#ffbb66", "asleep": "#a69cc4",
          "dictation": "#ff68cb"}


class TopBar(QWidget):
    listening_changed = Signal(bool)
    mode_changed = Signal(str)
    system_event = Signal(str)
    expanded_changed = Signal(bool)
    expanded_height = 180

    def __init__(self, settings=None):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.settings = {"monitor": "primary", "dwell_ms": 450, "idle_ms": 4000,
                         **(settings or {})}
        self._pill = self.settings.get("style") == "pill"
        if self._pill:
            self.expanded_height = 182
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAutoFillBackground(False)
        self.setWindowTitle("Voicebar")
        self.setAccessibleName("Voicebar voice controls")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFont(QFont("Segoe UI", 10))
        self.feedback = {"enabled": False, "stream_open": False, "mode": "name",
                         "dictating": False,
                         "conversation_enabled": False, "conversation_active": False,
                         "state": "off", "text": "", "detail": "Loading local speech model…",
                         "latency_ms": None}
        self.is_expanded = False
        self._height = self.collapsed_height
        self._hover_at = None
        self._last_activity = time.monotonic()
        self._screen = None
        self._screens = []
        self._notifications = None
        self.title = QLabel("VOICEBAR", self)
        self.title.setStyleSheet("color:#cbd5e1; font-weight:700; letter-spacing:2px")
        self.status = QLabel(self)
        self.heard = QLabel(self)
        self.detail = QLabel(self)
        self.detail.setWordWrap(True)
        self.latency = QLabel(self)
        self.listen_button = QPushButton("Off", self)
        self.listen_button.setCheckable(True)
        self.listen_button.clicked.connect(self.listening_changed)
        self.mode_button = QPushButton("NAME MODE", self)
        self.mode_button.clicked.connect(
            lambda: self.mode_changed.emit("open" if self.feedback["mode"] == "name" else "name"))
        self.hide_button = QPushButton("⌃", self)
        self.hide_button.clicked.connect(self.collapse)
        for widget in (self.listen_button, self.mode_button, self.hide_button):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
        for widget in (self.status, self.heard, self.detail, self.latency):
            widget.setTextFormat(Qt.TextFormat.PlainText)
            widget.setStyleSheet("color:#e2e8f0; background:transparent")
        self.heard.setStyleSheet("color:#f8fafc; font-size:15px")
        self.latency.setStyleSheet("color:#94a3b8; font-size:11px")
        for widget, name in (
            (self.status, "Microphone and listening mode"),
            (self.heard, "Heard command"), (self.detail, "Command result"),
            (self.latency, "Command latency"), (self.listen_button, "Toggle microphone"),
            (self.mode_button, "Change listening mode"), (self.hide_button, "Collapse Voicebar"),
        ):
            widget.setAccessibleName(name)
        if self._pill:
            self.title.setText("Voicebar")
            self.hide_button.setText("−")
            self.title.setStyleSheet("color:#f5f5f5; background:transparent; font-weight:600")
            self.status.setStyleSheet("color:#ededee; background:transparent; font-size:12px")
            self.heard.setStyleSheet("color:#ffffff; background:transparent; font-size:17px")
            self.detail.setStyleSheet("color:#e0e0e2; background:transparent; font-size:12px")
            self.latency.setStyleSheet("color:#cacacc; background:transparent; font-size:10px")
        self.animation = QPropertyAnimation(self, b"slideHeight", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        app.primaryScreenChanged.connect(self._screens_changed)
        self._screens_changed()
        self.set_feedback({})

    def _get_height(self):
        return self._height

    def _set_height(self, value):
        self._height = value
        if self._pill:
            self.reposition()
        else:
            self.resize(self.width(), value)
            self._layout()
        self.update()

    slideHeight = Property(int, _get_height, _set_height)

    @property
    def is_dictating(self):
        return self.feedback["dictating"] or self.feedback["state"] == "dictation"

    @property
    def collapsed_height(self):
        if self._pill:
            return 42
        if self.is_dictating and self.feedback["stream_open"]:
            return 16
        return 10 if self.feedback["mode"] == "open" else 6

    def _screens_changed(self, *_):
        current = QGuiApplication.screens()
        for screen in current:
            if screen not in self._screens:
                screen.geometryChanged.connect(self.reposition)
                screen.availableGeometryChanged.connect(self.reposition)
                screen.logicalDotsPerInchChanged.connect(self.reposition)
        self._screens = current
        self.reposition()

    def reposition(self, *_):
        screens = QGuiApplication.screens()
        selected = next((s for s in screens if s.name() == self.settings["monitor"]), None)
        screen = selected or QGuiApplication.primaryScreen()
        if screen is None:
            return
        self._screen = screen
        rect = screen.geometry()
        if self._pill:
            progress = max(0, min(1, (self._height - self.collapsed_height)
                                 / (self.expanded_height - self.collapsed_height)))
            width = min(360 + round(200 * progress), max(1, rect.width() - 16))
            top = rect.y() + 8
        else:
            width, top = min(620, rect.width()), rect.y()
        self.setGeometry(rect.x() + (rect.width() - width) // 2, top, width, self._height)
        self._layout()

    def _layout(self):
        if self._pill:
            self._layout_pill()
            return
        width = self.width()
        self.title.setGeometry(20, 16, 120, 26)
        self.mode_button.setGeometry(width - 290, 14, 132, 29)
        self.listen_button.setGeometry(width - 148, 14, 95, 29)
        self.hide_button.setGeometry(width - 43, 14, 25, 29)
        self.status.setGeometry(20, 50, width - 40, 22)
        self.heard.setGeometry(20, 77, width - 40, 26)
        self.detail.setGeometry(20, 106, width - 40, 43)
        self.latency.setGeometry(20, 151, width - 40, 20)
        for child in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            child.setVisible(self.is_expanded)

    def _layout_pill(self):
        width = self.width()
        self.title.setGeometry(34, 8, 110, 26)
        self.mode_button.setGeometry(width - 260, 8, 132, 26)
        self.listen_button.setGeometry(width - 118, 8, 76, 26)
        self.hide_button.setGeometry(width - 34, 8, 22, 26)
        self.status.setGeometry(18, 44, width - 36, 20)
        self.heard.setGeometry(18, 76, width - 36, 28)
        self.detail.setGeometry(18, 110, width - 36, 38)
        self.latency.setGeometry(18, 153, width - 36, 18)
        for child in (self.title, self.mode_button, self.hide_button,
                      self.heard, self.detail, self.latency):
            child.setVisible(self.is_expanded)
        self.mode_button.setVisible(self.is_expanded and not self.feedback["conversation_enabled"])
        # The title makes room for the controls during the width animation.
        self.title.setVisible(self.is_expanded and width >= 440)
        self.status.setVisible(True)
        self.listen_button.setVisible(True)
        if not self.is_expanded:
            self.status.setGeometry(34, 0, max(1, width - 138), self.collapsed_height)
            self.listen_button.setGeometry(width - 96, 8, 80, 26)
        self._set_pill_status()
        self._set_heard_text()

    def _set_pill_status(self):
        f = self.feedback
        if f["stream_open"]:
            state = "Microphone live"
            if self.is_dictating:
                state += " · Dictation"
            elif f["state"] in {"off", "locked"}:
                state += " · Closing"
            elif f["state"] == "asleep":
                state += " · Asleep"
            elif f["conversation_enabled"]:
                if f["state"] in {"hearing", "working"}:
                    state += " · " + str(f["state"]).capitalize()
                else:
                    state += " · " + ("Ready" if f["conversation_active"] else "Wake name needed")
            elif f["mode"] == "open":
                state += " · Open mode"
            elif f["state"] not in {"idle", "listening"}:
                state += " · " + str(f["state"]).replace("_", " ").capitalize()
        else:
            state = "Microphone closed"
            if f["state"] == "locked":
                state += " · Locked"
            elif f["enabled"]:
                state += " · Starting"
        self.status.setText(state)
        self.status.setToolTip(state)
        self.status.setAccessibleDescription(state)

    def _set_heard_text(self):
        text = self.feedback["text"] or (
            "Say a command" if self._pill else "Say “computer, open Notepad”")
        width = self.heard.width() if self._pill else self.width() - 40
        self.heard.setText(self.heard.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, max(1, width)))
        self.heard.setToolTip(self.feedback["text"])
        self.heard.setAccessibleDescription(self.feedback["text"])

    def _color(self):
        f = self.feedback
        if not f["stream_open"]:
            return QColor(COLORS["off"])
        if self.is_dictating:
            return QColor(COLORS["dictation"])
        if f["state"] in {"off", "locked"}:
            return QColor("#ff6868")
        return QColor("#ffbb66" if f["mode"] == "open" else COLORS.get(f["state"], COLORS["idle"]))

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._pill:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(255, 255, 255, 48), 1))
            painter.setBrush(QColor(48, 49, 52, 226))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 20, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color())
            painter.drawEllipse(17, 17, 8, 8)
            if self.is_expanded and self.height() > 70:
                painter.setPen(QPen(QColor(255, 255, 255, 28), 1))
                painter.drawLine(18, 69, self.width() - 18, 69)
            return
        painter.fillRect(self.rect(), QColor("#111827"))
        color = self._color()
        painter.fillRect(0, 0, self.width(), min(self.height(), self.collapsed_height), color)
        if not self.is_expanded and self.feedback["mode"] == "open":
            painter.setPen(QPen(QColor("#111827"), 2))
            for x in range(0, self.width(), 16):
                painter.drawLine(x, 0, x + 10, self.height())
        if not self.is_expanded and self.is_dictating and self.feedback["stream_open"]:
            painter.setPen(QColor("#111827"))
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "DICTATION · MICROPHONE LIVE")

    def set_feedback(self, row):
        previous_text = self.feedback["text"]
        self.feedback.update({key: row[key] for key in self.feedback if key in row})
        f = self.feedback
        live = bool(f["stream_open"])
        self.listen_button.setChecked(bool(f["enabled"]))
        self.listen_button.setText("Listening" if live else "Starting…" if f["enabled"] else "Off")
        self.mode_button.setText("SESSION" if self._pill and f["conversation_enabled"] else
                                 "OPEN MODE" if f["mode"] == "open" else "NAME MODE")
        button_style = "QPushButton {border:1px solid #475569;border-radius:8px;color:#e2e8f0;padding:2px}"
        self.listen_button.setStyleSheet(button_style + ("QPushButton {background:#145346}" if live else ""))
        self.mode_button.setStyleSheet(button_style + ("QPushButton {background:#775020}" if f["mode"] == "open" else ""))
        self.hide_button.setStyleSheet(button_style)
        if self._pill:
            pill_button = (
                "QPushButton {border:1px solid rgba(255,255,255,32);border-radius:10px;"
                "color:#f5f5f5;background:rgba(255,255,255,14);padding:2px;font-size:11px}"
                "QPushButton:hover {background:rgba(255,255,255,30)}"
            )
            self.listen_button.setStyleSheet(pill_button + (
                "QPushButton {background:rgba(56,214,173,35)}" if live else ""))
            self.mode_button.setStyleSheet(pill_button + (
                "QPushButton {background:rgba(255,187,102,35)}" if f["mode"] == "open" else ""))
            self.hide_button.setStyleSheet(pill_button)
        state = str(f["state"]).replace("_", " ")
        if self.is_dictating and state != "dictation":
            state = "dictation · " + state
        self.status.setText(f"{'● MICROPHONE LIVE' if live else '○ MICROPHONE CLOSED'}   ·   {state.upper()}")
        if self._pill:
            self._set_pill_status()
            self.mode_button.setVisible(self.is_expanded and not f["conversation_enabled"])
        self._set_heard_text()
        self.detail.setText(str(f["detail"]))
        if row.get("candidates"):
            self.detail.setText(str(f["detail"]) + " · Nearest: " + ", ".join(
                candidate["name"] for candidate in row["candidates"][:2]))
        self.detail.setToolTip(self.detail.text())
        self.detail.setAccessibleDescription(self.detail.text())
        ms = f["latency_ms"]
        self.latency.setText("End to end: —" if ms is None else f"End to end: {ms:,.0f} ms")
        heard = self._pill and bool(row.get("text")) and row["text"] != previous_text
        if (row.get("event") in {"decision", "error", "failure", "timeout"}
                or heard or (self._pill and row.get("state") == "hearing")):
            self.expand()
        if not self.is_expanded:
            self._set_height(self.collapsed_height)
        self.update()

    def expand(self):
        self._animate(True)

    def collapse(self):
        self._animate(False)

    def toggle(self):
        self._animate(not self.is_expanded)

    def _animate(self, expanded):
        self._last_activity = time.monotonic()
        self._hover_at = None
        self.is_expanded = expanded
        self.animation.stop()
        self.animation.setStartValue(self.height())
        self.animation.setEndValue(self.expanded_height if expanded else self.collapsed_height)
        self._layout()
        self.animation.start()
        self.expanded_changed.emit(expanded)

    def _tick(self):
        now = time.monotonic()
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        if inside:
            self._last_activity = now
            if not self.is_expanded:
                if self._hover_at is None:
                    self._hover_at = now
                elif (now - self._hover_at) * 1000 >= self.settings["dwell_ms"]:
                    self.expand()
        else:
            self._hover_at = None
            if (self.is_expanded and self.feedback["state"] not in {"hearing", "working"}
                    and (now - self._last_activity) * 1000 >= self.settings["idle_ms"]):
                self.collapse()

    def showEvent(self, event):
        if sys.platform == "win32":
            from .native import SessionNotifications, apply_no_activate
            apply_no_activate(int(self.winId()))
            if self._notifications is None:
                self._notifications = SessionNotifications(int(self.winId()))
        super().showEvent(event)

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            from ctypes import wintypes

            from .native import handle_window_message
            msg = wintypes.MSG.from_address(int(message))
            handled, result, action = handle_window_message(msg.message, msg.wParam)
            if action:
                self.system_event.emit(action)
                if action == "resume":
                    QTimer.singleShot(0, self.reposition)
            if handled:
                return True, result
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if self._notifications is not None:
            self._notifications.close()
            self._notifications = None
        super().closeEvent(event)
