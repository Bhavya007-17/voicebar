"""Dwell, status and placement behavior with a deterministic pointer clock."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect

from voicebar.ui.bar import TopBar
from voicebar.ui.settings import load_ui_settings


@pytest.fixture
def bar(qapp):
    widget = TopBar()
    widget.timer.stop()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()


def test_short_pointer_crossing_does_not_expand_but_continuous_dwell_does(bar, monkeypatch):
    now = [10.0]
    position = [bar.mapToGlobal(QPoint(10, 2))]
    monkeypatch.setattr("voicebar.ui.bar.time.monotonic", lambda: now[0])
    monkeypatch.setattr("voicebar.ui.bar.QCursor", SimpleNamespace(pos=lambda: position[0]))
    bar._tick()
    now[0] += .3
    bar._tick()
    assert not bar.is_expanded
    position[0] = bar.mapToGlobal(QPoint(10, 1000))
    bar._tick()
    position[0] = bar.mapToGlobal(QPoint(10, 2))
    bar._tick()
    now[0] += .449
    bar._tick()
    assert not bar.is_expanded
    now[0] += .002
    bar._tick()
    assert bar.is_expanded


def test_idle_collapse_waits_for_work_to_finish(bar, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("voicebar.ui.bar.time.monotonic", lambda: now[0])
    monkeypatch.setattr("voicebar.ui.bar.QCursor", SimpleNamespace(
        pos=lambda: bar.mapToGlobal(QPoint(10, 1000))))
    bar.expand()
    bar.set_feedback({"state": "working"})
    now[0] += 10
    bar._tick()
    assert bar.is_expanded
    bar.set_feedback({"state": "done"})
    bar._tick()
    assert not bar.is_expanded


def test_microphone_colour_uses_confirmed_stream_state_and_modes_are_prominent(bar):
    bar.set_feedback({"enabled": False, "stream_open": True, "state": "off"})
    assert bar._color().name() == "#ff6868", "Unconfirmed stream closure must stay a live warning"
    bar.set_feedback({"enabled": True, "stream_open": False, "state": "idle"})
    assert bar._color().name() == "#64748b"
    bar.set_feedback({"stream_open": True, "mode": "open", "state": "idle"})
    assert bar.collapsed_height == 10 and bar._color().name() == "#ffbb66"
    bar.set_feedback({"state": "dictation"})
    assert bar.collapsed_height == 16 and bar._color().name() == "#ff68cb"
    bar.set_feedback({"dictating": True, "state": "working"})
    assert bar.collapsed_height == 16 and bar._color().name() == "#ff68cb"
    assert "DICTATION" in bar.status.text()


def test_unknown_target_displays_two_nearest_candidates_and_plain_text(bar):
    bar.set_feedback({"event": "decision", "text": "computer open <unknown>",
                      "detail": "Unknown target", "state": "not sure", "latency_ms": 1234,
                      "candidates": [{"name": "Notepad"}, {"name": "Calculator"}]})
    assert "Notepad, Calculator" in bar.detail.text()
    assert "<unknown>" in bar.heard.text()
    assert "1,234 ms" in bar.latency.text()


def test_display_placement_uses_selected_logical_geometry_and_falls_back_then_recovers(bar, monkeypatch):
    primary = SimpleNamespace(name=lambda: "laptop", geometry=lambda: QRect(0, 0, 1536, 864))
    camera = SimpleNamespace(name=lambda: "camera", geometry=lambda: QRect(-1920, -120, 1920, 1080))
    current = [primary, camera]
    monkeypatch.setattr("voicebar.ui.bar.QGuiApplication", SimpleNamespace(
        screens=lambda: current, primaryScreen=lambda: primary))
    bar.settings["monitor"] = "camera"
    bar.reposition()
    assert bar.x() == -1920 + (1920 - bar.width()) // 2 and bar.y() == -120
    current.remove(camera)
    bar.reposition()
    assert bar.x() == (1536 - bar.width()) // 2 and bar.y() == 0
    current.append(camera)
    bar.reposition()
    assert bar.y() == -120


@pytest.mark.parametrize("config", ["dwell_ms: 0", "monitor: null", "idle_ms: .inf",
                                    "toggle_bar: ctrl+alt+f9", "unknown: 1"])
def test_invalid_ui_settings_fail_closed(tmp_path, config):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("ui:\n  " + config + "\n")
    with pytest.raises(ValueError):
        load_ui_settings(tmp_path)
