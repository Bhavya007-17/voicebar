"""Soak uses the configured local recognizer and never authorizes execution."""

import json
from types import SimpleNamespace

import pytest

from voicebar import cli


@pytest.fixture
def configured_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text(
        "mode: name\nstt:\n  backend: qwen\n  device: cuda\n", encoding="utf-8"
    )
    (tmp_path / "logs").mkdir()
    loaded = SimpleNamespace(catalog=object())
    monkeypatch.setattr(cli, "load_catalog", lambda *a, **k: loaded)
    return tmp_path, loaded


@pytest.mark.parametrize("mode", ["name", "open"])
def test_soak_default_command_is_dry_and_persists_all_trigger_utterances(
    configured_root, monkeypatch, capsys, mode
):
    root, loaded = configured_root
    calls = []
    triggers = [{"text": "computer open Notepad", "reason": "run",
                 "plan": [{"action": "open_app", "app": "Notepad"}], "dry_run": True}]
    log_path = root / "logs/listen-test.jsonl"
    outcome = {"complete": True, "status": "complete", "stop_reason": "duration",
               "elapsed_seconds": 600, "would_run": triggers, "decision_count": 42,
               "reason_counts": {"ignored": 41, "run": 1}}

    def listen(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(gate=SimpleNamespace(mode=mode), would_run=triggers,
                               outcome=outcome), log_path

    monkeypatch.setattr("voicebar.listening.listen", listen)
    assert cli.main(["soak", "--minutes", "10"], project_root=root) == 0
    assert calls == [((root, loaded), {"backend_name": "qwen", "device": "cuda",
                                     "dry_run": True, "minutes": 10.0})]
    report = json.loads(log_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report == {**outcome, "mode": mode, "minutes": 10.0, "would_run": triggers,
                      "log": str(log_path), "executor": "dry-run"}
    terminal = capsys.readouterr().out
    assert "SOAK REPORT" in terminal and triggers[0]["text"] in terminal
    assert "1 utterances would run" in terminal


@pytest.mark.parametrize("reason,status,exit_code", [
    ("worker_failure", "failed", 2), ("interrupted", "interrupted", 130),
])
def test_failed_or_interrupted_soak_saves_frozen_partial_results(
    configured_root, monkeypatch, capsys, reason, status, exit_code
):
    from voicebar.listening import ListeningFailure

    root, _ = configured_root
    log_path = root / "logs/listen-failed.jsonl"
    triggers = [{"text": "computer open Notepad", "reason": "run", "dry_run": True}]
    outcome = {"complete": False, "status": status, "stop_reason": reason,
               "elapsed_seconds": 229, "would_run": list(triggers), "decision_count": 46,
               "reason_counts": {"ignored": 45, "run": 1},
               "failures": [{"phase": "segmentation", "type": "RuntimeError",
                             "message": "Recognition backlog exceeded its bound", "at": 100}],
               "pending": {"segments": 8, "recognition_in_flight": True},
               "cleanup": {"stream_open": False, "workers_alive": ["voicebar-recognition"]}}
    session = SimpleNamespace(gate=SimpleNamespace(mode="name"), would_run=triggers, outcome=outcome)

    def failed_listen(*args, **kwargs):
        # A late native result must not change the finalized report snapshot.
        triggers.append({"text": "late speech", "reason": "stale_or_closed"})
        raise ListeningFailure(session, log_path, outcome, RuntimeError("backlog failure"))

    monkeypatch.setattr("voicebar.listening.listen", failed_listen)
    assert cli.main(["soak", "--minutes", "10"], project_root=root) == exit_code
    report = json.loads(log_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["complete"] is False and report["minutes"] == 10
    assert report["elapsed_seconds"] == 229 and report["decision_count"] == 46
    assert len(report["would_run"]) == 1
    assert report["pending"]["segments"] == 8 and not report["cleanup"]["stream_open"]
    terminal = capsys.readouterr()
    assert "INCOMPLETE SOAK REPORT" in terminal.out
    assert "the requested soak did not complete" in terminal.out
    assert "Recognition backlog exceeded its bound" in terminal.err


def test_report_save_failure_preserves_original_listener_error(configured_root, monkeypatch, capsys):
    from voicebar.listening import ListeningFailure

    root, _ = configured_root
    outcome = {"complete": False, "stop_reason": "worker_failure",
               "failures": [{"phase": "capture", "type": "RuntimeError",
                             "message": "original capture failure", "at": 100}]}
    session = SimpleNamespace(gate=SimpleNamespace(mode="name"), would_run=[], outcome=outcome)

    def failed_listen(*args, **kwargs):
        raise ListeningFailure(session, root / "logs/listen-failed.jsonl", outcome,
                               RuntimeError("original capture failure"))

    def failed_save(*args, **kwargs):
        raise OSError("report disk failure")

    monkeypatch.setattr("voicebar.listening.listen", failed_listen)
    monkeypatch.setattr("voicebar.soak.save_report", failed_save)
    assert cli.main(["soak", "--minutes", "10"], project_root=root) == 2
    terminal = capsys.readouterr()
    assert "original capture failure" in terminal.err and "report disk failure" in terminal.err
    assert "SOAK REPORT" not in terminal.out


def test_empty_run_without_completion_evidence_cannot_pass(configured_root, monkeypatch, capsys):
    root, _ = configured_root
    log_path = root / "logs/listen-unknown.jsonl"
    session = SimpleNamespace(gate=SimpleNamespace(mode="name"), would_run=[])
    monkeypatch.setattr("voicebar.listening.listen", lambda *a, **k: (session, log_path))
    assert cli.main(["soak", "--minutes", "10"], project_root=root) == 2
    report = json.loads(log_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert not report["complete"] and report["stop_reason"] == "missing_outcome"
    assert "INCOMPLETE SOAK REPORT" in capsys.readouterr().out


def test_failed_atomic_report_preserves_existing_file_and_removes_temporary(
    configured_root, monkeypatch
):
    from voicebar import soak

    root, _ = configured_root
    log_path = root / "logs/listen-test.jsonl"
    report_path = log_path.with_suffix(".report.json")
    report_path.write_text('{"previous": true}\n', encoding="utf-8")
    session = SimpleNamespace(gate=SimpleNamespace(mode="name"), would_run=[],
                              outcome={"complete": False})

    def failed_flush(descriptor):
        raise OSError("disk write failed")

    monkeypatch.setattr(soak.os, "fsync", failed_flush)
    with pytest.raises(OSError, match="disk write failed"):
        soak.save_report(session, log_path, 10)
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"previous": True}
    assert list(report_path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("minutes", ["0", "-1", "nan", "inf"])
def test_invalid_soak_duration_refuses_before_audio(configured_root, monkeypatch, capsys, minutes):
    root, _ = configured_root
    monkeypatch.setattr("voicebar.listening.listen", lambda *a, **k: pytest.fail("Audio opened"))
    assert cli.main(["soak", "--minutes", minutes], project_root=root) == 2
    assert "finite and greater than zero" in capsys.readouterr().err


def test_configured_listen_uses_gate_without_ptt(configured_root, monkeypatch):
    root, loaded = configured_root
    calls = []

    def listen(*args, **kwargs):
        calls.append((args, kwargs))
        return object(), root / "logs/listen-test.jsonl"

    monkeypatch.setattr("voicebar.listening.listen", listen)
    monkeypatch.setattr("voicebar.ptt.listen_ptt", lambda *a, **k: pytest.fail("PTT started"))
    assert cli.main(["listen"], project_root=root) == 0
    assert calls == [((root, loaded), {"backend_name": "qwen", "device": "cuda",
                                     "dry_run": False, "minutes": None})]
