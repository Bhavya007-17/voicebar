"""Failure recovery uses fake microphones, models and executors only."""

from test_ui_controller import Harness, event


def test_recognizer_exception_closes_stream_and_revokes_actions(tmp_path):
    harness = Harness(tmp_path)
    try:
        def fail(audio, emit):
            raise RuntimeError("owned recognizer failure")
        harness.backend.transcribe_segment = fail
        harness.ready()
        harness.controller.set_enabled(True)
        microphone = harness.transition("opened")
        microphone.speak()
        row = event(harness.controller, lambda row: "recognition stopped" in row.get("detail", ""))
        assert "recognition stopped" in row["detail"]
        assert harness.transition("closed") is microphone
        assert not harness.calls
    finally:
        harness.close()


def test_microphone_unplug_frame_error_closes_stream_without_actions(tmp_path):
    harness = Harness(tmp_path)
    try:
        harness.ready()
        harness.controller.set_enabled(True)
        microphone = harness.transition("opened")
        microphone.callback(b"", 0, "device disconnected")
        row = event(harness.controller, lambda row: "device disconnected" in row.get("detail", ""))
        assert "device disconnected" in row["detail"]
        assert harness.transition("closed") is microphone
        assert not harness.calls
    finally:
        harness.close()


def test_duplicate_instance_signals_only_its_named_activation_event():
    import uuid

    import pytest

    from voicebar.ui.native import SingleInstance
    name = "Local\\Voicebar.Stage8.Test." + uuid.uuid4().hex
    first = SingleInstance(name=name)
    unrelated = SingleInstance(name=name + ".Unrelated")
    try:
        assert not first.activation_requested()
        with pytest.raises(RuntimeError, match="already running"):
            SingleInstance(name=name)
        assert first.activation_requested()
        assert not first.activation_requested()
        assert not unrelated.activation_requested()
    finally:
        first.close()
        unrelated.close()


def test_new_bluetooth_input_is_discovered_while_off_and_selected_explicitly(tmp_path):
    devices = [{"index": 1, "name": "Owned default microphone"}]
    harness = Harness(tmp_path, devices=lambda: list(devices))
    try:
        harness.ready()
        devices.append({"index": 7, "name": "Owned Bluetooth headset"})
        harness.controller.refresh_microphones()
        row = event(harness.controller, lambda row: row.get("event") == "microphones"
                    and any(device["index"] == 7 for device in row.get("devices", [])))
        assert row["enabled"] is False and harness.transitions.empty()
        harness.controller.set_microphone(7)
        event(harness.controller, lambda row: row.get("microphone") == 7)
        assert harness.transitions.empty()
        harness.controller.set_enabled(True)
        assert harness.transition("opened").index == 7
    finally:
        harness.close()
