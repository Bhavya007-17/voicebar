"""Owned, hidden-widget checks for the translucent pill; no desktop input."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QImage

from voicebar.ui.bar import TopBar


@pytest.fixture
def pill(qapp, monkeypatch):
    def forbid_show(*_):
        pytest.fail("Pill unit tests must not show a native window")

    monkeypatch.setattr(TopBar, "showEvent", forbid_show)
    widget = TopBar({"style": "pill"})
    widget.timer.stop()
    try:
        yield widget
    finally:
        widget.animation.stop()
        widget.close()
        widget.deleteLater()
        qapp.processEvents()


def finish_animation(pill):
    pill.animation.setCurrentTime(pill.animation.duration())


@pytest.mark.parametrize("expanded", [False, True])
def test_pill_paints_translucent_gray_with_clear_rounded_corners(pill, expanded):
    if expanded:
        pill.expand()
        finish_animation(pill)
    image = QImage(pill.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    pill.render(image)
    assert image.pixelColor(0, 0).alpha() == 0
    surface = image.pixelColor(pill.width() // 2, pill.height() - 6)
    assert 150 < surface.alpha() < 255, "The background must have real per-pixel transparency"
    assert max(surface.red(), surface.green(), surface.blue()) - min(
        surface.red(), surface.green(), surface.blue()) < 12, "The surface must be neutral gray"


def test_pill_keeps_no_focus_contract_and_accessible_controls(pill):
    assert pill.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert pill.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert pill.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert pill.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert pill.windowFlags() & Qt.WindowType.Tool
    assert pill.focusPolicy() == Qt.FocusPolicy.NoFocus
    for control in (pill.listen_button, pill.mode_button, pill.hide_button):
        assert control.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert control.accessibleName()
    assert not pill.status.isHidden()
    assert not pill.listen_button.isHidden()
    assert pill.heard.isHidden()


def test_compact_state_reports_actual_microphone_state_and_open_modes(pill):
    pill.set_feedback({"enabled": True, "stream_open": False, "state": "idle"})
    assert "Microphone closed" in pill.status.text()
    pill.set_feedback({"enabled": False, "stream_open": True, "state": "off"})
    assert "Microphone live" in pill.status.text()
    assert "Closing" in pill.status.text()
    assert pill._color().name() == "#ff6868"
    pill.set_feedback({"enabled": True, "stream_open": True, "state": "idle", "mode": "open"})
    assert "Open mode" in pill.status.text()
    pill.set_feedback({"dictating": True, "state": "working"})
    assert "Dictation" in pill.status.text()
    assert "Microphone live" in pill.status.text()
    assert pill.status.accessibleDescription() == pill.status.text()
    assert pill.collapsed_height >= 40


def test_new_transcript_expands_and_preserves_full_accessible_text(pill):
    transcript = "Search for <high five> and its history " * 12
    pill.set_feedback({"text": transcript, "state": "working"})
    finish_animation(pill)
    assert pill.is_expanded and not pill.heard.isHidden()
    assert pill.heard.textFormat() == Qt.TextFormat.PlainText
    assert pill.heard.toolTip() == transcript
    assert pill.heard.accessibleDescription() == transcript
    assert pill.heard.text().endswith("…")
    pill.collapse()
    finish_animation(pill)
    pill.set_feedback({"text": transcript, "state": "done"})
    assert not pill.is_expanded, "Repeated feedback must not reopen a dismissed transcript"


def test_conversation_session_readiness_is_visible_and_legacy_mode_is_hidden(pill):
    pill.set_feedback({"enabled": True, "stream_open": True, "state": "idle",
                       "conversation_enabled": True, "conversation_active": True})
    assert pill.status.text() == "Microphone live · Ready"
    pill.expand()
    finish_animation(pill)
    assert pill.mode_button.isHidden() and pill.mode_button.text() == "SESSION"
    pill.set_feedback({"conversation_active": False})
    assert "Microphone live" in pill.status.text()
    assert "Wake name needed" in pill.status.text()
    assert pill.mode_button.isHidden()
    pill.set_feedback({"conversation_enabled": False})
    assert not pill.mode_button.isHidden()
    assert pill.mode_button.text() == "NAME MODE"


def test_decision_expands_result_and_refreshes_transcript_for_expanded_width(pill):
    transcript = "Calculator: type 2 plus 3"
    pill.set_feedback({"event": "decision", "text": transcript,
                       "state": "done", "detail": "Opened Calculator"})
    finish_animation(pill)
    assert pill.is_expanded and pill.height() == pill.expanded_height
    assert pill.heard.text() == transcript
    assert pill.detail.text() == "Opened Calculator"
    assert pill.detail.accessibleDescription() == "Opened Calculator"


def test_pill_resizes_around_selected_screen_center_and_recovers_after_unplug(pill, monkeypatch):
    primary = SimpleNamespace(name=lambda: "laptop", geometry=lambda: QRect(0, 0, 1536, 864))
    second = SimpleNamespace(name=lambda: "second", geometry=lambda: QRect(-1920, -120, 1920, 1080))
    current = [primary, second]
    monkeypatch.setattr("voicebar.ui.bar.QGuiApplication", SimpleNamespace(
        screens=lambda: current, primaryScreen=lambda: primary))
    pill.settings["monitor"] = "second"
    pill.reposition()
    compact_width = pill.width()
    assert pill.x() == -1920 + (1920 - compact_width) // 2
    assert pill.y() == -112
    pill.expand()
    finish_animation(pill)
    assert pill.width() > compact_width
    assert pill.x() == -1920 + (1920 - pill.width()) // 2
    current.remove(second)
    pill.reposition()
    assert pill.x() == (1536 - pill.width()) // 2 and pill.y() == 8
    current.append(second)
    pill.reposition()
    assert pill.y() == -112


def test_pill_dwell_and_idle_collapse_retain_microphone_indicator(pill, monkeypatch):
    now = [10.0]
    pointer = [pill.mapToGlobal(QPoint(50, 20))]
    monkeypatch.setattr("voicebar.ui.bar.time.monotonic", lambda: now[0])
    monkeypatch.setattr("voicebar.ui.bar.QCursor", SimpleNamespace(pos=lambda: pointer[0]))
    pill._tick()
    now[0] += .46
    pill._tick()
    finish_animation(pill)
    assert pill.is_expanded
    pointer[0] = pill.mapToGlobal(QPoint(50, 1000))
    now[0] += 5
    pill._tick()
    finish_animation(pill)
    assert not pill.is_expanded
    assert not pill.status.isHidden() and not pill.listen_button.isHidden()
    assert "Microphone closed" in pill.status.text()
