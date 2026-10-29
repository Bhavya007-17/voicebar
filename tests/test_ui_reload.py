"""Reload/shutdown races use bounded fake resources and explicit event barriers."""

import queue
import threading
from copy import deepcopy

import yaml
from test_ui_controller import Backend, Harness, event

from voicebar.configuration import DEFAULT_SETTINGS, ReloadResult
from voicebar.resolver import configured_resolver, resolve
from voicebar.ui.controller import RuntimeController


class OwnedBackend(Backend):
    def __init__(self, text="computer open Notepad", *, broken_close=False):
        super().__init__()
        self.text = text
        self.close_calls = 0
        self.disposed = threading.Event()
        self.broken_close = broken_close

    def close(self):
        self.close_calls += 1
        self.disposed.set()
        if self.broken_close:
            raise RuntimeError("owned fake native disposal failed")
        super().close()


def make_harness(root, monkeypatch, *, selected="moonshine", **options):
    # Harness's fake devices remain unchanged. Pause its automatic start so
    # initial-null configuration is installed before a worker can observe it.
    with monkeypatch.context() as setup:
        setup.setattr(RuntimeController, "start", lambda self: None)
        harness = Harness(root, **options)
    document = deepcopy(DEFAULT_SETTINGS)
    document["stt"] = {"backend": selected, "device": "cpu"}
    (root / "config/settings.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    harness.controller.backend_name = selected
    harness.controller._initial_settings = document
    harness.controller.start()
    return harness


def reload_model(harness, selected, **changes):
    document = deepcopy(harness.controller._initial_settings)
    document.update(changes)
    document["stt"] = {"backend": selected, "device": "cpu"}
    result = ReloadResult(harness.controller.loaded, document, ("settings.yaml",))
    harness.controller._submit("config", result)
    event(harness.controller, lambda row: row.get("event") == "config_reloaded")


def test_initial_null_selection_recovers_after_live_config_without_restart(tmp_path, monkeypatch):
    harness = make_harness(tmp_path, monkeypatch, selected=None)
    try:
        row = event(harness.controller, lambda row: row.get("event") == "error"
                    and "No speech backend selected" in row.get("detail", ""))
        assert not row["stream_open"] and not harness.controller._fatal
        reload_model(harness, "moonshine")
        harness.ready()
        harness.controller.set_enabled(True)
        harness.transition("opened").speak()
        event(harness.controller, lambda row: row.get("event") == "decision")
        assert harness.calls == [{"action": "open_app", "app": "Notepad"}]
    finally:
        harness.close()


def test_initial_load_and_rapid_changes_keep_only_latest_pending_model(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    first = OwnedBackend()
    latest = OwnedBackend("computer open Calculator")
    calls = []

    def factory(name, *args, **kwargs):
        calls.append(name)
        if name == "moonshine":
            entered.set()
            assert release.wait(3), "Test did not release initial fake model"
            return first
        assert name == "parakeet", "Superseded pending model must never initialize"
        assert first.close_calls == 1, "Old resources must close before another model allocates"
        return latest

    harness = make_harness(tmp_path, monkeypatch, backend_factory=factory)
    try:
        assert entered.wait(3)
        reload_model(harness, "qwen")
        reload_model(harness, "parakeet")
        release.set()
        harness.ready()
        assert calls == ["moonshine", "parakeet"]
        assert harness.controller._backend is latest
        harness.controller.set_enabled(True)
        harness.transition("opened").speak()
        event(harness.controller, lambda row: row.get("event") == "decision")
        assert harness.calls == [{"action": "open_app", "app": "Calculator"}]
    finally:
        release.set()
        harness.close()
    assert first.close_calls == latest.close_calls == 1


def test_queued_failure_from_superseded_model_cannot_revoke_new_state(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    first, replacement = OwnedBackend(), OwnedBackend()

    def factory(name, *args, **kwargs):
        if name == "moonshine":
            return first
        entered.set()
        assert release.wait(3), "Test did not release replacement fake model"
        return replacement

    harness = make_harness(tmp_path, monkeypatch, backend_factory=factory)
    try:
        harness.ready()
        previous_token = harness.controller._reload_token
        reload_model(harness, "parakeet")
        assert entered.wait(3)
        harness.controller.set_enabled(True)
        event(harness.controller, lambda row: row.get("event") == "control")
        harness.controller._submit("model_failed", (previous_token, "stale fake initialization error"))
        harness.controller.set_mode("name")
        row = event(harness.controller, lambda row: row.get("event") == "control")
        assert row["enabled"] is True
        assert not harness.controller.ready.is_set() and not harness.controller._fatal
        release.set()
        harness.ready()
        harness.transition("opened")
    finally:
        release.set()
        harness.close()


def test_shutdown_during_replacement_disposes_each_model_once_without_publishing_it(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    first, replacement = OwnedBackend(), OwnedBackend()

    def factory(name, *args, **kwargs):
        if name == "moonshine":
            return first
        entered.set()
        assert release.wait(3), "Test did not release replacement fake model"
        return replacement

    harness = make_harness(tmp_path, monkeypatch, backend_factory=factory)
    try:
        harness.ready()
        reload_model(harness, "parakeet")
        assert entered.wait(3) and first.close_calls == 1
        harness.close()
        row = event(harness.controller, lambda row: row.get("event") == "stopped")
        assert "voicebar-model-loader" in row["workers_alive"]
        assert replacement.close_calls == 0
        release.set()
        assert replacement.disposed.wait(3)
        assert first.close_calls == replacement.close_calls == 1
        assert harness.controller._backend is None
        assert not harness.controller.ready.is_set()
    finally:
        release.set()
        harness.close()


def test_native_close_failure_is_visible_and_cannot_kill_shutdown(tmp_path, monkeypatch):
    first = OwnedBackend(broken_close=True)
    calls = []

    def factory(name, *args, **kwargs):
        calls.append(name)
        return first

    harness = make_harness(tmp_path, monkeypatch, backend_factory=factory)
    try:
        harness.ready()
        reload_model(harness, "parakeet")
        row = event(harness.controller, lambda row: row.get("event") == "error"
                    and "Speech model cleanup failed" in row.get("detail", ""))
        assert row["enabled"] is False and harness.controller._fatal
        assert calls == ["moonshine"]
    finally:
        harness.close()
    assert first.close_calls == 1


def test_final_log_failure_does_not_leave_quit_waiting(tmp_path, monkeypatch):
    harness = make_harness(tmp_path, monkeypatch)
    harness.ready()
    original = harness.controller._log

    def log(row):
        if row.get("event") == "runtime_stopped":
            raise OSError("owned fake disk is full")
        original(row)

    harness.controller._log = log
    harness.close()
    rows = []
    while True:
        try:
            rows.append(harness.controller.events.get_nowait())
        except queue.Empty:
            break
    assert any(row.get("event") == "error" and "Final decision log" in row.get("detail", "") for row in rows)
    assert any(row.get("event") == "stopped" for row in rows)
    assert harness.controller.closed.is_set()


def test_model_reload_restores_same_resolver_from_validated_snapshot(tmp_path, monkeypatch):
    prepared = []

    def configure(root, *, document=None):
        selector = lambda *args, **kwargs: None
        prepared.append((document, selector))
        return selector

    monkeypatch.setattr("voicebar.resolver.configured_resolver", configure)
    harness = make_harness(tmp_path, monkeypatch)
    try:
        harness.ready()
        reload_model(harness, "parakeet")
        harness.ready()
        assert len(prepared) == 2
        assert all(document["resolver"]["model"] is None for document, _ in prepared)
        assert harness.controller.session.resolver is prepared[-1][1]
        assert harness.controller.session.resolver is not resolve
    finally:
        harness.close()


def test_disabled_resolver_uses_supplied_snapshot_without_reading_disk(tmp_path):
    assert configured_resolver(tmp_path, document={"resolver": {"model": None}}) is resolve


def test_optional_warmup_finishes_before_ready_and_microphone_open(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class WarmBackend(OwnedBackend):
        def warmup(self):
            entered.set()
            assert release.wait(3), "Test did not release owned fake warmup"

    backend = WarmBackend()
    harness = make_harness(tmp_path, monkeypatch, backend_factory=lambda *args, **kwargs: backend)
    try:
        assert entered.wait(3)
        assert not harness.controller.ready.is_set()
        harness.controller.set_enabled(True)
        event(harness.controller, lambda row: row.get("event") == "control")
        assert harness.transitions.empty()
        release.set()
        harness.ready()
        harness.transition("opened")
    finally:
        release.set()
        harness.close()
    assert backend.close_calls == 1


def test_close_during_warmup_never_publishes_model_after_shutdown(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class WarmBackend(OwnedBackend):
        def warmup(self):
            entered.set()
            assert release.wait(3), "Test did not release owned fake warmup"

    backend = WarmBackend()
    harness = make_harness(tmp_path, monkeypatch, backend_factory=lambda *args, **kwargs: backend)
    try:
        assert entered.wait(3)
        harness.close()
        assert not harness.controller.ready.is_set()
        release.set()
        assert backend.disposed.wait(3)
        assert harness.controller._backend is None and backend.close_calls == 1
    finally:
        release.set()
        harness.close()
