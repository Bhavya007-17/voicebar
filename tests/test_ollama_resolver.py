"""The resolver transport tests use fake HTTP objects, never listening sockets."""
import json
from types import SimpleNamespace

import pytest

from voicebar.resolver import configured_resolver
from voicebar.resolver.ollama import OllamaBackend


def info(count=3_000_000_000, **extra):
    return {"model_info": {"general.parameter_count": count},
            "capabilities": ["completion"], **extra}


@pytest.mark.parametrize("model", ["", "model", "remote:cloud", "https://host/model", "bad tag:1"])
def test_model_must_be_explicit_local_tag(model):
    with pytest.raises(ValueError):
        OllamaBackend(model)


@pytest.mark.parametrize("metadata", [info(4_000_000_001), info(True), info(0), {},
    info(remote_host="remote"), info(remote_model="remote"),
    {"model_info": {"general.parameter_count": 3}, "capabilities": []}])
def test_metadata_refusal_sends_no_speech(metadata, monkeypatch):
    backend = OllamaBackend("local:small")
    calls = []
    def request(path, body=None, **kwargs):
        calls.append((path, body))
        return metadata
    monkeypatch.setattr(backend, "_request", request)
    with pytest.raises(ValueError):
        backend.select(SimpleNamespace(prompt="private speech", schema={}), 1.5)
    assert calls == [("/api/show", {"model": "local:small"})]


def test_selection_structured_deterministic_and_persistent(monkeypatch):
    backend = OllamaBackend("local:small")
    calls = []
    def request(path, body=None, **kwargs):
        calls.append((path, body))
        return info() if path == "/api/show" else {
            "done": True, "done_reason": "stop", "response": '{"actions":["none"]}'}
    monkeypatch.setattr(backend, "_request", request)
    schema = {"type": "object"}
    assert backend.select(SimpleNamespace(prompt="Schema and choices", schema=schema), 1.5)
    payload = calls[-1][1]
    assert payload["format"] == schema
    assert payload["options"]["temperature"] == 0
    assert payload["stream"] is False and payload["keep_alive"] == -1
    assert payload["think"] is False
    assert backend.warm()
    assert "prompt" not in calls[-1][1]
    assert calls[-1][1]["options"] == payload["options"]
    assert calls[-1][1]["think"] is False
    assert calls[-1][1]["options"]["num_ctx"] == 8192
    backend.unload()
    assert calls[-1][1] == {"model": "local:small", "stream": False, "keep_alive": 0}


@pytest.mark.parametrize("raw,status", [
    (b'{"response":"x","response":"y"}', 200),
    (b"x" * 524289, 200), (b"[]", 200), (b"{}", 302),
], ids=["duplicate", "oversized", "array", "redirect"])
def test_fixed_loopback_no_redirects_bounded_and_closed(raw, status, monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))
        def request(self, *args, **kwargs):
            calls.append(args)
        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda limit: raw[:limit])
        def close(self):
            calls.append("closed")
    monkeypatch.setattr("voicebar.resolver.ollama.http.client.HTTPConnection", Connection)
    with pytest.raises((ValueError, TypeError)):
        OllamaBackend("local:small")._request("/api/version")
    assert calls[0] == ("127.0.0.1", 11434, 1.5)
    assert calls[-1] == "closed"


def test_unselected_configuration_has_no_transport(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("resolver: {model: null}\n")
    monkeypatch.setattr("voicebar.resolver.ollama.http.client.HTTPConnection",
                        lambda *a, **k: pytest.fail("unselected resolver connected"))
    assert configured_resolver(tmp_path)("computer loose speech", None, {}) is None


def test_http_uses_json_and_reads_local_metadata(monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1"
        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))
        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda n: json.dumps(info()).encode())
        def close(self):
            pass
    monkeypatch.setattr("voicebar.resolver.ollama.http.client.HTTPConnection", Connection)
    assert OllamaBackend("local:small").local_model()
    assert calls[0][0:3] == ("POST", "/api/show", {"model": "local:small"})


@pytest.mark.parametrize("first_warm_fails", [False, True])
def test_configured_preload_gates_selection_and_retries_offline(tmp_path, monkeypatch, first_warm_fails):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("resolver: {model: 'local:small'}\n")
    jobs, selections, warm_calls = [], [], []

    class ControlledThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            jobs.append(self.target)

    class FakeBackend:
        def __init__(self, *args, **kwargs):
            pass

        def warm(self):
            warm_calls.append(True)
            if first_warm_fails and len(warm_calls) == 1:
                raise ConnectionRefusedError
            return True

    class FakeResolver:
        last_outcome = "resolved"

        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, *args):
            selections.append(args)
            return [{"action": "volume", "op": "mute"}]

    monkeypatch.setattr("voicebar.resolver.threading.Thread", ControlledThread)
    monkeypatch.setattr("voicebar.resolver.ollama.OllamaBackend", FakeBackend)
    monkeypatch.setattr("voicebar.resolver.base.Resolver", FakeResolver)
    fallback = configured_resolver(tmp_path)
    assert len(jobs) == 1
    assert fallback("computer quiet please", None) is None
    assert len(jobs) == 1 and selections == []
    jobs.pop(0)()
    if first_warm_fails:
        assert fallback("computer quiet please", None) is None
        assert len(jobs) == 1 and selections == []
        jobs.pop(0)()
    assert fallback("computer quiet please", None) == [{"action": "volume", "op": "mute"}]
    assert len(selections) == 1


def test_configured_transport_failure_requires_preload_again(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("resolver: {model: 'local:small'}\n")
    jobs = []
    class ControlledThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            jobs.append(self.target)
    class FakeBackend:
        def __init__(self, *args, **kwargs):
            pass
        def warm(self):
            return True
    class FakeResolver:
        last_outcome = "backend_error"
        def __init__(self, *args, **kwargs):
            pass
        def resolve(self, *args):
            return None
    monkeypatch.setattr("voicebar.resolver.threading.Thread", ControlledThread)
    monkeypatch.setattr("voicebar.resolver.ollama.OllamaBackend", FakeBackend)
    monkeypatch.setattr("voicebar.resolver.base.Resolver", FakeResolver)
    fallback = configured_resolver(tmp_path)
    jobs.pop(0)()
    assert fallback("computer quiet please", None) is None
    assert jobs == []
    assert fallback("computer quiet please", None) is None
    assert len(jobs) == 1
