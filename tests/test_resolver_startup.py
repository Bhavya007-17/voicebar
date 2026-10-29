"""Speech reserves its memory before the optional selector starts preloading."""

import threading

from test_ui_controller import Harness


def test_ui_defers_selector_preload_until_speech_backend_is_loaded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    order = []

    def speech_factory(*args, **kwargs):
        order.append("speech_loading")
        entered.set()
        assert release.wait(3)
        order.append("speech_loaded")
        return harness.backend

    def selector_factory(root, *, document=None):
        assert order[-1] == "speech_loaded"
        assert document["stt"]["backend"] == "fake"
        order.append("selector_preload")
        return lambda *args: None

    monkeypatch.setattr("voicebar.resolver.configured_resolver", selector_factory)
    harness = Harness(tmp_path, backend_factory=speech_factory, autostart=False)
    try:
        assert not order
        harness.controller.start()
        assert entered.wait(3)
        assert order == ["speech_loading"]
        release.set()
        assert harness.controller.ready.wait(3)
        assert order == ["speech_loading", "speech_loaded", "selector_preload"]
    finally:
        release.set()
        harness.close()
