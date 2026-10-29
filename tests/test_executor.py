"""Dispatcher preflight, stop-on-failure and ownership guard contracts."""

from types import SimpleNamespace

import pytest

from voicebar.executor import execute
from voicebar.executor.common import Context, ExecutionError, load_settings
from voicebar.schema import SchemaError


class FakeContext:
    def save(self):
        pass


def test_invalid_tail_has_no_side_effects(monkeypatch):
    monkeypatch.setattr("voicebar.executor._handlers", lambda: pytest.fail("must validate first"))
    with pytest.raises(SchemaError):
        execute([{"action": "press", "keys": "f11"}, {"action": "delete_everything"}],
                context=FakeContext())


def test_failure_stops_remaining_plan(monkeypatch):
    calls = []

    def fail(ctx, action):
        calls.append(action)
        raise ExecutionError("foreground activation refused")

    monkeypatch.setattr("voicebar.executor._handlers", lambda: {"press": fail})
    result = execute([{"action": "press", "keys": "f11"},
                      {"action": "press", "keys": "enter"}], context=FakeContext())
    assert len(calls) == 1
    assert [item["ok"] for item in result] == [False, False]
    assert "Skipped" in result[1]["detail"]
    assert result[0]["started_ms"] is None


def test_reserved_tail_has_no_partial_execution(monkeypatch):
    monkeypatch.setattr("voicebar.executor._handlers", lambda: pytest.fail("must preflight reserved tail"))
    result = execute([{"action": "open_app", "app": "Notepad"},
                      {"action": "mode", "op": "sleep"}], context=FakeContext())
    assert len(result) == 2
    assert all(not item["ok"] and item["started_ms"] is None for item in result)


def context(native, owned=None):
    ctx = object.__new__(Context)
    ctx.native, ctx.owned = native, owned
    return ctx


def test_recycled_hwnd_is_rejected():
    ctx = context(SimpleNamespace(is_window=lambda hwnd, pid: False))
    with pytest.raises(ExecutionError, match="identity changed"):
        ctx.guard(SimpleNamespace(hwnd=10, pid=20))


def test_unowned_foreground_is_rejected_before_token_query():
    ctx = context(SimpleNamespace(is_window=lambda hwnd, pid: True), {11: 20})
    with pytest.raises(ExecutionError, match="did not open"):
        ctx.guard(SimpleNamespace(hwnd=10, pid=20))


def test_elevated_target_is_rejected():
    ctx = context(SimpleNamespace(is_window=lambda hwnd, pid: True,
                                  integrity=lambda pid: 0x3000,
                                  current_integrity=lambda: 0x2000))
    with pytest.raises(ExecutionError, match="elevated"):
        ctx.guard(SimpleNamespace(hwnd=10, pid=20))


def test_missing_integrity_is_not_assumed_safe():
    def denied(pid):
        raise PermissionError("access denied")

    ctx = context(SimpleNamespace(is_window=lambda hwnd, pid: True, integrity=denied))
    with pytest.raises(ExecutionError, match="Cannot check target integrity"):
        ctx.guard(SimpleNamespace(hwnd=10, pid=20))


def test_executor_settings_reject_unknown_app(tmp_path):
    from voicebar.catalog import CatalogError
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("executor:\n  new_window_apps: [Missing]\n")
    loaded = SimpleNamespace(catalog=SimpleNamespace(apps=["Notepad"]))
    with pytest.raises(CatalogError, match="missing or ambiguous"):
        load_settings(tmp_path, loaded)
