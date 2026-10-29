"""Vision transport uses fake HTTP only: no images captured or models launched."""
import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from voicebar.resolver.vision import MAX_CANDIDATES, MAX_IMAGE_BYTES, VisionSelector

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX6sAAAAASUVORK5CYII="
)
ROWS = [{"name": "Calendar", "role": "button"}, {"name": "Assignments", "role": "link"}]


def metadata(**extra):
    return {"model_info": {"general.parameter_count": 2_000_000_000},
            "capabilities": ["completion", "vision"], **extra}


def answer(content='{"id":2}', **extra):
    return {"done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": content}, **extra}


def selector(monkeypatch, *, info=None, reply=None):
    selected = VisionSelector()
    calls = []

    def request(path, body=None, *, timeout_s):
        calls.append((path, body, timeout_s))
        if path == "/api/show":
            return metadata() if info is None else info
        return answer() if reply is None else reply

    monkeypatch.setattr(selected.backend, "_request", request)
    return selected, calls


def test_selects_only_code_assigned_integer_and_bounds_schema(monkeypatch):
    selected, calls = selector(monkeypatch)
    assert selected.select("open today's assignments", PNG, ROWS) == 2
    assert selected.last_outcome == "resolved"
    assert [call[0] for call in calls] == ["/api/show", "/api/chat"]
    assert calls[0][1] == {"model": "qwen3-vl:2b-instruct"}
    payload = calls[1][1]
    assert payload["model"] == "qwen3-vl:2b-instruct"
    assert payload["format"] == {
        "type": "object", "properties": {"id": {"enum": [None, 1, 2]}},
        "required": ["id"], "additionalProperties": False,
    }
    assert payload["options"]["temperature"] == 0
    assert payload["stream"] is False and payload["think"] is False
    assert payload["keep_alive"] == -1  # Idle eviction must not put cold loading on a spoken command.
    assert payload["options"]["num_ctx"] == 4096
    assert payload["messages"][0]["role"] == "system"
    assert "untrusted" in payload["messages"][0]["content"]
    user = payload["messages"][1]
    assert base64.b64decode(user["images"][0]) == PNG
    assert json.loads(user["content"])["candidates"] == [
        {"id": 1, **ROWS[0]}, {"id": 2, **ROWS[1]},
    ]
    assert 0 < calls[1][2] <= calls[0][2] <= 5


def test_accepts_existing_code_assigned_sequential_ids(monkeypatch):
    selected, calls = selector(monkeypatch)
    rows = [{"id": index, **row} for index, row in enumerate(ROWS, 1)]
    assert selected.select("open assignments", PNG, rows) == 2
    prompt = json.loads(calls[1][1]["messages"][1]["content"])
    assert prompt["candidates"] == rows


@pytest.mark.parametrize("identifier", [True, "1", 1.0, 0, 2])
def test_candidate_ids_cannot_override_the_code_order(monkeypatch, identifier):
    selected, calls = selector(monkeypatch)
    assert selected.select("open calendar", PNG, [{"id": identifier, **ROWS[0]}]) is None
    assert calls == []


@pytest.mark.parametrize("content", [
    '{"id":null}', '{"id":0}', '{"id":3}', '{"id":-1}', '{"id":true}',
    '{"id":2.0}', '{"id":"2"}', '{"id":[2]}', '{"id":{"id":2}}',
    '{"id":1,"id":2}', '{"id":2,"action":"click"}', '{"actions":[2]}',
    '{"id":NaN}', '{"id":Infinity}', '{"id":null,"x":{"id":1,"id":2}}',
    '```json\n{"id":2}\n```', '{"id":2} trailing', '[2]', 'null', '',
    ' ' * 257, '{"id":2,"coords":[10,20]}',
], ids=[f"output-{index}" for index in range(22)])
def test_invalid_or_abstaining_output_never_selects(monkeypatch, content):
    selected, _ = selector(monkeypatch, reply=answer(content))
    assert selected.select("open assignments", PNG, ROWS) is None


@pytest.mark.parametrize("reply", [
    answer(done=False), answer(done_reason="length"),
    answer(message={"role": "user", "content": '{"id":2}'}),
    answer(message={"role": "assistant", "content": '{"id":2}',
                    "tool_calls": [{"function": {"name": "click"}}]}),
    answer(message=None), {},
])
def test_incomplete_or_nonassistant_reply_refused(monkeypatch, reply):
    selected, _ = selector(monkeypatch, reply=reply)
    assert selected.select("open assignments", PNG, ROWS) is None


@pytest.mark.parametrize("info", [
    metadata(remote_host="cloud.example"), metadata(remote_model="remote"),
    metadata(capabilities=["completion"]), metadata(capabilities=["vision"]),
    metadata(capabilities="completion vision"), metadata(capabilities=None),
    metadata(model_info={"general.parameter_count": 4_000_000_001}),
    metadata(model_info={"general.parameter_count": True}), {},
])
def test_unverified_model_receives_no_screenshot_or_speech(monkeypatch, info):
    selected, calls = selector(monkeypatch, info=info)
    assert selected.select("private speech", PNG, ROWS) is None
    assert [(path, body) for path, body, _ in calls] == [
        ("/api/show", {"model": "qwen3-vl:2b-instruct"}),
    ]


@pytest.mark.parametrize("model", ["remote:cloud", "qwen3-vl", "https://host/model:2b"])
def test_remote_or_untagged_model_configuration_refused(model):
    with pytest.raises(ValueError):
        VisionSelector(model)


@pytest.mark.parametrize("text,image,rows", [
    ("", PNG, ROWS), ("x" * 4097, PNG, ROWS), (None, PNG, ROWS),
    ("open", b"not a png", ROWS), ("open", PNG.decode("latin1"), ROWS),
    ("open", PNG + b"x" * MAX_IMAGE_BYTES, ROWS),
    ("open", PNG[:16] + (9000).to_bytes(4, "big") + PNG[20:], ROWS),
    ("open", PNG[:16] + (8192).to_bytes(4, "big") * 2 + PNG[24:], ROWS),
    ("open", PNG, []), ("open", PNG, ROWS * MAX_CANDIDATES),
    ("open", PNG, [{"name": "Calendar", "role": "button", "id": 7}]),
    ("open", PNG, [{"name": "Calendar", "role": "button", "action": "click"}]),
    ("open", PNG, [{"name": "", "role": "button"}]),
    ("open", PNG, [{"name": "x" * 513, "role": "button"}]),
    ("open", PNG, [{"name": "Calendar", "role": "x" * 65}]),
    ("open", PNG, [{"name": 12, "role": "button"}]),
], ids=[f"input-{index}" for index in range(16)])
def test_invalid_or_unbounded_input_has_no_transport(monkeypatch, text, image, rows):
    selected, calls = selector(monkeypatch)
    assert selected.select(text, image, rows) is None
    assert calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, None, "5", float("nan"), float("inf"), 61])
def test_invalid_deadline_has_no_transport(monkeypatch, timeout):
    selected, calls = selector(monkeypatch)
    assert selected.select("open assignments", PNG, ROWS, timeout_s=timeout) is None
    assert calls == []
    assert selected.last_outcome == "invalid_input"


def test_serialized_request_size_checked_before_image_sent(monkeypatch):
    selected, calls = selector(monkeypatch)
    monkeypatch.setattr("voicebar.resolver.vision.MAX_REQUEST_BYTES", 1)
    assert selected.select("open assignments", PNG, ROWS) is None
    assert len(calls) == 1 and calls[0][0] == "/api/show"


@pytest.mark.parametrize("stalled_path", ["/api/show", "/api/chat"])
def test_wall_clock_deadline_and_single_worker_even_if_transport_ignores_timeout(
        monkeypatch, stalled_path):
    selected = VisionSelector()
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def request(path, body=None, *, timeout_s):
        calls.append(path)
        if path == stalled_path:
            entered.set()
            release.wait(2)
        return metadata() if path == "/api/show" else answer()

    monkeypatch.setattr(selected.backend, "_request", request)
    started = time.monotonic()
    try:
        assert selected.select("open assignments", PNG, ROWS, timeout_s=0.03) is None
        assert time.monotonic() - started < 0.5
        assert entered.is_set() and selected.last_outcome == "timeout"
        count = len(calls)
        assert selected.select("open calendar", PNG, ROWS) is None
        assert selected.last_outcome == "busy" and len(calls) == count
    finally:
        release.set()
        assert selected.wait_idle(1)
    # A timed-out metadata check never proceeds to send the image.
    assert calls == (["/api/show"] if stalled_path == "/api/show"
                     else ["/api/show", "/api/chat"])


def test_warm_verifies_vision_and_uses_same_options_without_private_data(monkeypatch):
    selected, calls = selector(monkeypatch, reply=answer('{"id":null}'))
    assert selected.warm()
    assert [call[0] for call in calls] == ["/api/show", "/api/chat"]
    payload = calls[1][1]
    assert payload["format"]["properties"]["id"]["enum"] == [None]
    assert payload["keep_alive"] == -1
    user = payload["messages"][1]
    prompt = json.loads(user["content"])
    assert prompt["candidates"] == []
    assert "private" not in user["content"]
    synthetic = base64.b64decode(user["images"][0])
    assert synthetic.startswith(b"\x89PNG\r\n\x1a\n") and synthetic != PNG
    assert (int.from_bytes(synthetic[16:20], "big"), int.from_bytes(synthetic[20:24], "big")) == (64, 64)
    assert selected.select("open assignments", PNG, ROWS) is None
    assert calls[-1][1]["options"] == payload["options"]


@pytest.mark.parametrize("reply", [
    answer('{"id":1}'), answer('{"id":null,"id":null}'), answer('not JSON'),
    answer('{"id":null}', done=False), answer('{"id":null}', done_reason="length"),
    {"done": True},
])
def test_warm_requires_completed_null_only_image_response(monkeypatch, reply):
    selected, calls = selector(monkeypatch, reply=reply)
    assert not selected.warm()
    assert [call[0] for call in calls] == ["/api/show", "/api/chat"]


def test_warm_fixture_is_a_decodable_synthetic_image():
    from PySide6.QtGui import QImage

    from voicebar.resolver.vision import _WARM_IMAGE

    image = QImage.fromData(base64.b64decode(_WARM_IMAGE), "PNG")
    assert not image.isNull()
    assert (image.width(), image.height()) == (64, 64)
    assert image.pixelColor(0, 0) == image.pixelColor(63, 63)


def test_large_inference_image_is_bounded_without_changing_the_cached_png():
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    from voicebar.resolver.vision import _inference_image

    original = QImage(1600, 800, QImage.Format.Format_RGB32)
    original.fill(0xFF808080)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert original.save(buffer, "PNG")
    raw = bytes(buffer.data())
    buffer.close()
    bounded = QImage.fromData(_inference_image(raw), "PNG")
    assert (bounded.width(), bounded.height()) == (768, 384)
    assert QImage.fromData(raw, "PNG").width() == 1600


def test_warm_refuses_nonvision_before_loading(monkeypatch):
    selected, calls = selector(monkeypatch, info=metadata(capabilities=["completion"]))
    assert not selected.warm()
    assert len(calls) == 1


@pytest.mark.parametrize("raw,status", [
    (b'{"done":true,"done":false}', 200), (b"x" * 524289, 200),
    (b"[]", 200), (b"{}", 302),
], ids=["duplicate", "oversized", "array", "redirect"])
def test_existing_transport_is_fixed_loopback_bounded_strict_and_closed(monkeypatch, raw, status):
    calls = []

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body)))

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda limit: raw[:limit])

        def close(self):
            calls.append("closed")

    monkeypatch.setattr("voicebar.resolver.ollama.http.client.HTTPConnection", Connection)
    selected = VisionSelector(port=11435)
    assert selected.select("private speech", PNG, ROWS) is None
    assert calls[0][:2] == ("127.0.0.1", 11435)
    assert calls[1] == ("POST", "/api/show", {"model": "qwen3-vl:2b-instruct"})
    assert calls[-1] == "closed"


def test_worker_start_failure_releases_busy_state(monkeypatch):
    selected, calls = selector(monkeypatch)

    def fail_start(self):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr("voicebar.resolver.vision.threading.Thread.start", fail_start)
    assert selected.select("open assignments", PNG, ROWS) is None
    assert selected.wait_idle(0)
    assert calls == []
