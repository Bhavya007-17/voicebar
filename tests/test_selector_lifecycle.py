"""Selector lifecycle checks use fake transport and in-process workers only."""

import threading
from types import SimpleNamespace

import pytest
from test_ui_controller import Backend, Harness, event

from voicebar.resolver import _ManagedResolver, release_configured_models
from voicebar.resolver.ollama import OllamaBackend
from voicebar.ui.controller import RuntimeController

DOCUMENT = {"resolver": {"backend": "ollama", "model": "text:3b", "port": 11434},
            "vision": {"enabled": True, "model": "vision:2b"}}


def transport(monkeypatch, *, sticky=False):
    resident = {"text:3b", "vision:2b", "unrelated:7b"}
    calls = []

    def request(self, path, body=None, *, timeout_s):
        assert self.port == 11434 and 0 < timeout_s <= 10
        calls.append((path, body))
        if path == "/api/ps":
            return {"models": [{"name": name} for name in sorted(resident)]}
        assert path == "/api/generate" and body == {
            "model": self.model, "stream": False, "keep_alive": 0}
        if not sticky:
            resident.remove(self.model)
        return {"done": True}

    monkeypatch.setattr(OllamaBackend, "_request", request)
    return resident, calls


def test_only_configured_resident_models_are_released_and_confirmed(monkeypatch):
    resident, calls = transport(monkeypatch)
    assert release_configured_models(DOCUMENT) == [
        {"port": 11434, "model": "text:3b"}, {"port": 11434, "model": "vision:2b"}]
    assert resident == {"unrelated:7b"}
    assert [path for path, _ in calls] == ["/api/ps", "/api/generate", "/api/generate", "/api/ps"]


def test_disabled_vision_and_duplicate_tags_are_not_unloaded_twice(monkeypatch):
    resident, calls = transport(monkeypatch)
    duplicate = {"resolver": DOCUMENT["resolver"], "vision": {"enabled": True, "model": "text:3b"}}
    release_configured_models(duplicate)
    assert resident == {"vision:2b", "unrelated:7b"}
    assert sum(path == "/api/generate" for path, _ in calls) == 1
    calls.clear()
    release_configured_models({"vision": {"enabled": False}})
    assert not calls


def test_unconfirmed_release_blocks_preparation(monkeypatch):
    transport(monkeypatch, sticky=True)
    with pytest.raises(RuntimeError, match="remained resident"):
        release_configured_models(DOCUMENT)


def test_missing_optional_server_keeps_speech_available(monkeypatch):
    def refused(*args, **kwargs):
        raise ConnectionRefusedError
    monkeypatch.setattr(OllamaBackend, "_request", refused)
    assert release_configured_models(DOCUMENT) == []


def test_shared_deadline_and_cancellation_stop_additional_requests(monkeypatch):
    now, calls = [0.0], []
    monkeypatch.setattr("voicebar.resolver.time.monotonic", lambda: now[0])

    def request(*args, **kwargs):
        calls.append(True)
        now[0] = 11
        return {"models": [{"name": "text:3b"}]}
    monkeypatch.setattr(OllamaBackend, "_request", request)
    with pytest.raises(TimeoutError):
        release_configured_models(DOCUMENT)
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="cancelled"):
        release_configured_models(DOCUMENT, cancelled=lambda: True)
    assert len(calls) == 1


def managed(backend, *, automatic=False):
    resolver = SimpleNamespace(last_outcome="resolved", wait_idle=lambda timeout: True,
                               resolve=lambda *args: [{"action": "volume", "op": "mute"}])
    return _ManagedResolver(backend, resolver, automatic=automatic)


def test_manual_preparation_does_not_start_text_while_vision_warms():
    calls = []
    fallback = managed(SimpleNamespace(warm=lambda: calls.append("text") or True))
    assert fallback("please quiet", None) is None and not calls
    assert fallback.warm()
    assert fallback("please quiet", None) == [{"action": "volume", "op": "mute"}]
    assert calls == ["text"]


def test_stop_prevents_queued_preload_and_late_selection(monkeypatch):
    jobs, calls = [], []
    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            jobs.append(self.target)
    monkeypatch.setattr("voicebar.resolver.threading.Thread", DeferredThread)
    fallback = managed(SimpleNamespace(warm=lambda: calls.append(True) or True), automatic=True)
    fallback.start_preload()
    fallback.stop()
    jobs.pop()()
    assert fallback.wait_idle(.1)
    assert fallback("please quiet", None) is None
    assert not calls and not jobs


def test_wait_idle_tracks_running_warmup_and_stop_discards_its_result():
    entered, release = threading.Event(), threading.Event()
    def warm():
        entered.set()
        assert release.wait(3)
        return True
    fallback = managed(SimpleNamespace(warm=warm))
    worker = threading.Thread(target=fallback.warm)
    worker.start()
    try:
        assert entered.wait(3)
        fallback.stop()
        assert not fallback.wait_idle(.01)
        release.set()
        worker.join(3)
        assert not worker.is_alive() and fallback.wait_idle(.1)
        assert not fallback.ready.is_set() and fallback("please quiet", None) is None
    finally:
        release.set()
        worker.join(3)


def test_cuda_preflight_happens_before_speech_load(tmp_path, monkeypatch):
    order = []
    backend = Backend()
    monkeypatch.setattr("voicebar.resolver.release_configured_models",
                        lambda *args, **kwargs: order.append("release"))
    def load(*args, **kwargs):
        assert order == ["release"]
        order.append("speech")
        return backend
    harness = Harness(tmp_path, device="cuda", backend_factory=load)
    try:
        harness.ready()
        assert order == ["release", "speech"]
    finally:
        harness.close()


def test_failed_preflight_never_starts_speech_allocation(tmp_path, monkeypatch):
    calls = []
    def refused(*args, **kwargs):
        raise TimeoutError("unload was not confirmed")
    monkeypatch.setattr("voicebar.resolver.release_configured_models", refused)
    harness = Harness(tmp_path, device="cuda", backend_factory=lambda *args, **kwargs: calls.append(True))
    try:
        row = event(harness.controller, lambda row: row["event"] == "error"
                    and "unload was not confirmed" in row.get("detail", ""))
        assert "unload was not confirmed" in row["detail"]
        assert not calls and not harness.controller.ready.is_set()
    finally:
        harness.close()


def selector_controller():
    controller = RuntimeController.__new__(RuntimeController)
    controller._vision_settings = {"enabled": True, "model": "vision:2b", "port": 11434}
    controller._vision_selector = None
    controller._text_selector = None
    controller._selector_lock = threading.Lock()
    controller._selector_state = threading.Lock()
    controller._selector_worker = None
    controller._selector_pending = None
    controller._selector_epoch = 1
    controller._retired_selectors = []
    controller._resolver_settings = None
    controller._stop = threading.Event()
    controller._reload_token = 1
    controller.cleanup_timeout = .1
    controller.session = SimpleNamespace(resolver=None)
    controller._emit = lambda **row: None
    return controller


@pytest.mark.parametrize("retire", [False, True])
def test_vision_then_text_warm_and_stale_generation_cannot_reload(monkeypatch, retire):
    controller = selector_controller()
    jobs, order = [], []
    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            jobs.append(self.target)
    class Vision:
        def __init__(self, *args, **kwargs):
            pass
        def warm(self, **kwargs):
            order.append("vision")
            if retire:
                controller._retire_selectors()
            return True
        def wait_idle(self, timeout):
            order.append("vision_idle")
            return True
    controller._text_selector = managed(SimpleNamespace(warm=lambda: order.append("text") or True))
    monkeypatch.setattr("voicebar.ui.controller.threading.Thread", DeferredThread)
    monkeypatch.setattr("voicebar.resolver.vision.VisionSelector", Vision)
    controller._prepare_vision()
    jobs.pop()()
    # Retirement checks idle once without blocking; warmup then confirms its
    # transport finished before deciding whether the text job is still current.
    assert order == (["vision", "vision_idle", "vision_idle"] if retire
                     else ["vision", "vision_idle", "text"])


def test_busy_old_selector_blocks_cuda_release_without_losing_retired_handle(monkeypatch):
    controller = selector_controller()
    old = SimpleNamespace(wait_idle=lambda timeout: False)
    controller._retired_selectors = [old]
    monkeypatch.setattr("voicebar.resolver.release_configured_models", lambda *a, **k: pytest.fail("unload raced selection"))
    with pytest.raises(TimeoutError, match="Previous selector request"):
        controller._prepare_speech_models(1, "cuda", DOCUMENT)
    assert controller._retired_selectors == [old]
    assert controller._selector_lock.acquire(blocking=False)
    controller._selector_lock.release()


def test_exact_grounding_stays_available_while_model_preload_holds_lifecycle_lock(monkeypatch):
    controller = selector_controller()
    controller.ready = threading.Event()
    controller.ready.set()
    controller.root = "."
    controller.loaded = SimpleNamespace()
    controller._executor_settings = {}
    controller.screen_frames = object()
    controller._vision_settings["timeout_s"] = 5
    controller.session = SimpleNamespace(generation=1, capture_allowed=True,
                                         gate=SimpleNamespace(asleep=False))
    selected = []
    controller._vision_selector = SimpleNamespace(select=lambda *a, **k: selected.append(True) or 1)
    expected = [{"action": "app_action", "app": "active", "name": "click_role:button:Save"}]

    def exact_match(*args, **kwargs):
        assert kwargs["authorized"]()
        # Even if a visual fallback is attempted while warming, it cannot
        # block this caller or issue another model request.
        assert kwargs["selector"].select("save", b"", []) is None
        assert kwargs["selector"].last_outcome == "busy"
        return expected, (12, 34)

    monkeypatch.setattr("voicebar.screen.resolve_control", exact_match)
    controller._selector_lock.acquire()
    try:
        assert controller._grounded("click save") == expected
    finally:
        controller._selector_lock.release()
    assert selected == [] and controller._screen_target == (12, 34)


def test_reloads_replace_pending_preload_and_discard_idle_retired_handles(monkeypatch):
    controller = selector_controller()
    jobs, order = [], []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            jobs.append(self.target)

    class Vision:
        def __init__(self, model, **kwargs):
            self.model = model
        def warm(self, **kwargs):
            order.append(self.model)
            return True
        def wait_idle(self, timeout):
            return True

    monkeypatch.setattr("voicebar.ui.controller.threading.Thread", DeferredThread)
    monkeypatch.setattr("voicebar.resolver.vision.VisionSelector", Vision)
    for number in range(30):
        controller._retire_selectors()
        controller._vision_settings["model"] = f"vision:{number}"
        controller._text_selector = managed(SimpleNamespace(warm=lambda: order.append("text") or True))
        controller._prepare_vision()
        assert len(jobs) == 1
        assert controller._retired_selectors == []
    jobs.pop()()
    assert order == ["vision:29", "text"]
    assert controller._selector_worker is None and controller._selector_pending is None


def test_running_preload_only_runs_latest_replacement_and_keeps_single_worker(monkeypatch):
    controller = selector_controller()
    entered, release = threading.Event(), threading.Event()
    final = threading.Event()
    order = []

    class Vision:
        def __init__(self, model, **kwargs):
            self.model = model
            self.busy = threading.Event()
        def warm(self, **kwargs):
            self.busy.set()
            order.append(self.model)
            if self.model == "vision:2b":
                entered.set()
                assert release.wait(3)
            self.busy.clear()
            return True
        def wait_idle(self, timeout):
            return not self.busy.is_set()

    monkeypatch.setattr("voicebar.resolver.vision.VisionSelector", Vision)
    controller._text_selector = managed(SimpleNamespace(warm=lambda: pytest.fail("stale text warmed")))
    controller._prepare_vision()
    worker = controller._selector_worker
    try:
        assert entered.wait(3)
        for number in range(30):
            controller._retire_selectors()
            controller._vision_settings["model"] = f"vision:{number}"
            controller._text_selector = managed(SimpleNamespace(warm=lambda: final.set() or True))
            controller._prepare_vision()
            assert controller._selector_worker is worker
            assert len(controller._retired_selectors) == 1
        release.set()
        assert final.wait(3)
        worker.join(3)
        assert not worker.is_alive()
        assert order == ["vision:2b", "vision:29"]
        assert controller._retired_selectors == []
    finally:
        release.set()
        worker.join(3)


@pytest.mark.parametrize("outcome", ["timeout", "backend_error", "not_sure"])
def test_grounded_proxy_preserves_each_calls_outcome(monkeypatch, outcome):
    controller = selector_controller()
    controller.ready = threading.Event()
    controller.ready.set()
    controller.root, controller.loaded = ".", SimpleNamespace()
    controller._executor_settings, controller.screen_frames = {}, object()
    controller._vision_settings["timeout_s"] = 5
    controller.session = SimpleNamespace(generation=1, capture_allowed=True,
                                         gate=SimpleNamespace(asleep=False))
    selector = SimpleNamespace(last_outcome="not_started")
    def select(*args, **kwargs):
        selector.last_outcome = outcome
    selector.select = select
    controller._vision_selector = selector

    def resolve(*args, **kwargs):
        proxy = kwargs["selector"]
        assert proxy.last_outcome == "not_started"
        assert proxy.select("save", b"", []) is None
        selector.last_outcome = "resolved"
        assert proxy.last_outcome == outcome

    monkeypatch.setattr("voicebar.screen.resolve_control", resolve)
    assert controller._grounded("click save") is None


def test_stopped_selection_waits_for_dispatch_and_discards_late_plan():
    entered, release = threading.Event(), threading.Event()
    fallback = managed(SimpleNamespace(warm=lambda: True))
    assert fallback.warm()
    result = []

    def select(*args):
        entered.set()
        assert release.wait(3)
        return [{"action": "volume", "op": "mute"}]

    fallback.resolver.resolve = select
    worker = threading.Thread(target=lambda: result.append(fallback("quiet", None)))
    worker.start()
    try:
        assert entered.wait(3)
        # A concurrent request must abstain immediately instead of queuing
        # behind an in-flight fallback and receiving a fresh timeout later.
        assert fallback("second request", None) is None
        fallback.stop()
        assert not fallback.wait_idle(.01)
        release.set()
        worker.join(3)
        assert result == [None] and fallback.wait_idle(.1)
    finally:
        release.set()
        worker.join(3)
