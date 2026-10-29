"""Inspection and foreground-context integration use read-only fake adapters."""

import json
import threading
from types import SimpleNamespace

import pytest

from voicebar import cli, inspection
from voicebar.catalog import Catalog, LoadedCatalog


class Automation:
    def __init__(self, controls):
        self.controls = controls
        self.calls = []
        self.closed = False

    def snapshot(self, hwnd):
        self.calls.append(hwnd)
        return self.controls

    def close(self):
        self.closed = True


def test_inspection_writes_complete_tree_and_prints_only_clickable_controls(tmp_path):
    controls = [
        {"name": "Calculator", "role": "window", "automation_id": "",
         "keyboard_shortcut": "", "enabled": True, "clickable": False},
        {"name": "One", "role": "button", "automation_id": "num1Button",
         "keyboard_shortcut": "1", "enabled": True, "clickable": True},
    ]
    automation, lines = Automation(controls), []
    target = inspection.inspect_foreground(
        tmp_path, native=SimpleNamespace(foreground=lambda: 44), automation=automation,
        output_fn=lines.append)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert target.parent == tmp_path / "logs"
    assert document["hwnd"] == 44 and document["controls"] == controls
    assert document["timestamp"].endswith("+00:00")
    assert automation.calls == [44]
    assert "2 controls; 1 look clickable" in lines[1]
    assert json.loads(lines[2]) == {key: controls[1][key] for key in (
        "name", "role", "automation_id", "keyboard_shortcut", "enabled")}
    assert len(lines) == 3
    assert not automation.closed  # caller owns injected adapters


def test_no_foreground_does_not_write_or_query_ui_automation(tmp_path):
    automation = Automation([])
    with pytest.raises(RuntimeError, match="No foreground window"):
        inspection.inspect_foreground(tmp_path, native=SimpleNamespace(foreground=lambda: 0),
                                      automation=automation)
    assert automation.calls == []
    assert not (tmp_path / "logs").exists()


def test_owned_adapters_close_when_provider_query_fails(tmp_path, monkeypatch):
    from voicebar.executor import native, uia
    released = []
    fake_native = SimpleNamespace(foreground=lambda: 45, shutdown=lambda: released.append("native"))

    def failed_snapshot(hwnd):
        raise RuntimeError("Provider disappeared")

    fake_automation = SimpleNamespace(snapshot=failed_snapshot,
                                      close=lambda: released.append("automation"))
    monkeypatch.setattr(native, "NativeWindows", lambda: fake_native)
    monkeypatch.setattr(uia, "Automation", lambda: fake_automation)
    with pytest.raises(RuntimeError, match="Provider disappeared"):
        inspection.inspect_foreground(tmp_path)
    assert released == ["automation", "native"]
    assert not (tmp_path / "logs").exists()


def test_watch_prints_changed_pointer_controls_without_repetition():
    stop, lines = threading.Event(), []
    first = {"name": "One", "role": "button", "hwnd": 44, "pid": 5}
    second = {"name": "Two", "role": "button", "hwnd": 44, "pid": 5}
    values = iter([first, dict(first), second])
    calls = []

    def under_pointer():
        value = next(values)
        calls.append(value)
        if len(calls) == 3:
            stop.set()
        return value

    count = inspection.watch_controls(automation=SimpleNamespace(under_pointer=under_pointer),
                                     stop_event=stop, output_fn=lines.append, poll_interval=.0001)
    assert count == 2
    assert [json.loads(line) for line in lines[1:]] == [first, second]
    assert len(calls) == 3


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True])
def test_watch_rejects_invalid_deadline_before_provider_access(duration):
    with pytest.raises(ValueError, match="finite positive"):
        inspection.watch_controls(duration=duration,
                                  automation=SimpleNamespace(under_pointer=lambda: pytest.fail()))


def test_cli_inspect_does_not_need_catalog_and_passes_output(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_catalog", lambda *args, **kwargs: pytest.fail("No catalog query"))
    monkeypatch.setattr(inspection, "inspect_foreground",
                        lambda root, **kwargs: calls.append((root, kwargs)))
    output = tmp_path / "controls.json"
    assert cli.main(["inspect", "--output", str(output)], project_root=tmp_path) == 0
    assert calls == [(tmp_path, {"output": output})]


def test_cli_watch_passes_duration_and_rejects_snapshot_output(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(inspection, "watch_controls", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["inspect", "--watch", "--duration", "0.5"], project_root=tmp_path) == 0
    assert calls == [{"duration": 0.5}]
    assert cli.main(["inspect", "--watch", "--output", "ignored.json"], project_root=tmp_path) == 2
    assert "omit it with --watch" in capsys.readouterr().err
    assert len(calls) == 1


def test_cli_plan_uses_supplied_foreground_without_querying_windows(tmp_path, monkeypatch, capsys):
    catalog = Catalog(apps=["Claude"], app_actions={"Claude": {"new_chat": {
        "say": ["new chat"], "do": [{"keys": "ctrl+n"}],
    }}})
    loaded = LoadedCatalog(catalog, [], {}, [])
    monkeypatch.setattr(cli, "load_catalog", lambda *args, **kwargs: loaded)
    monkeypatch.setattr("voicebar.executor.common.Context", lambda *args, **kwargs: pytest.fail())
    assert cli.main(["plan", "--active-app", "Claude", "new chat"], project_root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == [{"action": "app_action", "app": "Claude", "name": "new_chat"}]


def test_controller_queries_foreground_only_with_definitions_and_emits_control_candidates(tmp_path):
    from voicebar.ui.controller import RuntimeController
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("name: computer\n", encoding="utf-8")
    catalog = Catalog(apps=["Notepad"])
    loaded, calls = LoadedCatalog(catalog, [], {}, []), []
    controller = RuntimeController(tmp_path, loaded,
                                   foreground_app=lambda: calls.append(True) or "Notepad")
    assert controller.session.foreground_app() is None
    assert not calls
    catalog.app_actions["Notepad"] = {"new_document": {"say": ["new document"], "do": [{"keys": "ctrl+n"}]}}
    assert controller.session.foreground_app() == "Notepad"
    assert calls == [True]
    while not controller.events.empty():
        controller.events.get_nowait()
    candidates = [{"name": "Close", "role": "button", "automation_id": "Close"}]
    controller._session_row({"event": "decision", "reason": "run", "text": "computer click close",
                             "timings": {}, "results": [{"ok": False, "detail": "Ambiguous control",
                                                           "candidates": candidates}]})
    row = controller.events.get_nowait()
    assert row["candidates"] == candidates
    assert row["state"] == "not sure" and row["detail"] == "Ambiguous control"


def test_ptt_bare_app_phrase_uses_only_explicit_foreground_callback(tmp_path, monkeypatch):
    from voicebar import ptt
    catalog = Catalog(apps=["Notepad"], app_actions={"Notepad": {"new_document": {
        "say": ["new document"], "do": [{"keys": "ctrl+n"}],
    }}})
    loaded = LoadedCatalog(catalog, [], {}, [])
    controls = SimpleNamespace(register=lambda: None, close=lambda: None,
                               wait_pressed=lambda stop: True, available=lambda: True,
                               held=lambda: True)
    backend = SimpleNamespace(name="fake", device="cpu", close=lambda: None)
    monkeypatch.setattr(ptt, "_hold", lambda *args, **kwargs: {
        "text": "new document", "status": "recognized", "released_at": None,
        "capture_closed": True, "executed": False, "execution_attempted": False,
    })
    queries = []
    report = ptt.listen_ptt(
        tmp_path, loaded, hotkeys=controls, microphone=object(),
        backend_factory=lambda *args, **kwargs: backend, state_reader=lambda: None,
        dry_run=True, max_utterances=1, output_fn=lambda line: None,
        foreground_app=lambda: queries.append(True) or "Notepad")
    assert queries == [True]
    assert report.last_result["plan"] == [{"action": "app_action", "app": "Notepad", "name": "new_document"}]
