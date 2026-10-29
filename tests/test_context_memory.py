"""Conversational execution keeps app context in memory, retaining test ownership."""

import json
from types import SimpleNamespace

from voicebar.executor.common import Context


def context(root, **options):
    return Context(root, loaded=SimpleNamespace(), settings={}, native=object(),
                   apps=object(), remember_app=False, **options)


def test_temporary_context_neither_reads_nor_overwrites_cli_app_memory(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEBAR_TEST_SESSION", raising=False)
    path = tmp_path / ".cache/runtime.json"
    path.parent.mkdir()
    original = '{"last_app": "Notepad"}'
    path.write_text(original)
    ctx = context(tmp_path)
    assert ctx.last_app is None and ctx.owned is None
    ctx.last_app = "Calculator"
    ctx.save()
    assert path.read_text() == original


def test_temporary_context_still_loads_and_preserves_explicit_test_ownership(tmp_path, monkeypatch):
    path = tmp_path / "owned.json"
    path.write_text(json.dumps({"last_app": "Notepad", "owned": {"123": 456}}))
    monkeypatch.setenv("VOICEBAR_TEST_SESSION", str(path))
    ctx = context(tmp_path)
    assert ctx.last_app is None and ctx.owned == {123: 456}
    ctx.last_app = "Calculator"
    ctx.save()
    assert json.loads(path.read_text()) == {"owned": {"123": 456}}
