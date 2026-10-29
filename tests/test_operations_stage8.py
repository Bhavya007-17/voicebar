"""Retention, transcript privacy and config publication without desktop effects."""

import json
import os

import pytest
import yaml

from voicebar import logs
from voicebar.configuration import (
    ConfigReloader,
    ensure_default_config,
    first_run_message,
)
from voicebar.doctor import print_doctor, run_doctor


@pytest.fixture
def project(tmp_path):
    ensure_default_config(tmp_path)
    (tmp_path / ".cache").mkdir(exist_ok=True)
    (tmp_path / ".cache/startapps.json").write_text(json.dumps([
        {"Name": "Notepad", "AppID": "notepad"},
        {"Name": "Calculator", "AppID": "calculator"},
    ]), encoding="utf-8")
    return tmp_path


def settings(root, **changes):
    path = root / "config/settings.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value.update(changes)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_defaults_are_local_conservative_and_never_overwrite_existing(project):
    path = project / "config/settings.yaml"
    original = path.read_bytes()
    assert ensure_default_config(project) == []
    assert path.read_bytes() == original
    document = yaml.safe_load(original)
    assert document["mode"] == "name"
    assert document["stt"]["backend"] is None
    assert document["resolver"]["model"] is None
    assert yaml.safe_load((project / "config/pinned.yaml").read_text()) == []
    assert "pin" in first_run_message([path])


def test_portable_init_keeps_exact_backup_of_machine_config(project):
    (project / "config/aliases.yaml").write_text("my editor: Notepad\n", encoding="utf-8")
    (project / "config/apps/test.yaml").write_text("machine specific", encoding="utf-8")
    ensure_default_config(project, portable=True)
    backups = list((project / ".cache/config-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "apps/test.yaml").read_text() == "machine specific"
    assert (backups[0] / "aliases.yaml").read_text() == "my editor: Notepad\n"
    assert yaml.safe_load((project / "config/aliases.yaml").read_text()) == {}


def test_privacy_removes_payloads_and_diagnostic_echoes_preserving_decision(project):
    settings(project, logs={"keep_transcripts": False})
    writer = logs.DecisionLog(project)
    writer({"event": "decision", "text": "private spoken phrase", "reason": "run",
            "plan": [{"action": "type_text", "text": "private typed phrase", "submit": True},
                     {"action": "web_search", "engine": "google", "query": "private query"}],
            "results": [{"ok": True, "detail": "private spoken phrase"}],
            "foreground_error": "private spoken phrase", "timings": {"gate_ms": 0.3}})
    raw = writer.path.read_text(encoding="utf-8")
    row = json.loads(raw)
    assert "private" not in raw
    assert row["reason"] == "run"
    assert row["plan"] == [{"action": "type_text", "submit": True},
                           {"action": "web_search", "engine": "google"}]
    assert row["results"] == [{"ok": True}]
    assert row["timings"] == {"gate_ms": 0.3}
    assert row["transcripts_stored"] is False


def test_rotation_and_total_byte_cap_include_prior_sessions_and_reports(project):
    settings(project, logs={"max_file_bytes": 1024, "max_total_bytes": 2048})
    directory = project / "logs"
    directory.mkdir()
    (directory / "old.report.json").write_text("x" * 1500, encoding="utf-8")
    writer = logs.DecisionLog(project)
    for index in range(50):
        writer({"event": "decision", "reason": "ignored", "sequence": index, "text": "x" * 250})
    paths = [path for path in directory.iterdir() if path.is_file()]
    assert sum(path.stat().st_size for path in paths) <= 2048
    assert all(path.stat().st_size <= 1024 for path in paths)
    assert not (directory / "old.report.json").exists()
    assert logs.read_recent(project)[-1]["sequence"] == 49
    assert any("part" in path.name for path in paths)


def test_policy_reloads_without_restarting_and_invalid_save_hides_text(project, monkeypatch):
    calls = []
    original = logs.load_log_settings
    monkeypatch.setattr(logs, "load_log_settings", lambda root: calls.append(True) or original(root))
    writer = logs.DecisionLog(project)
    writer({"text": "first"})
    writer({"text": "second"})
    calls_after_first = len(calls)
    writer({"text": "third"})
    assert len(calls) == calls_after_first
    settings(project, logs={"keep_transcripts": False})
    writer({"text": "now hidden", "reason": "ignored"})
    assert "now hidden" not in writer.path.read_text(encoding="utf-8")
    (project / "config/settings.yaml").write_text("logs: [broken]\n", encoding="utf-8")
    writer({"text": "still hidden", "reason": "ignored"})
    assert "still hidden" not in writer.path.read_text(encoding="utf-8")


def test_recent_orders_interleaved_files_and_ignores_broken_tail(project):
    directory = project / "logs"
    directory.mkdir()
    a, b = directory / "listen-a.jsonl", directory / "ptt-b.jsonl"
    a.write_text(json.dumps({"time": "2026-01-01T03:00:00Z", "order": 3}) + "\n{incomplete", encoding="utf-8")
    b.write_text("\n".join(json.dumps({"timestamp": f"2026-01-01T0{i}:00:00Z", "order": i})
                           for i in (1, 2)) + "\n", encoding="utf-8")
    os.utime(a, (1, 1))
    os.utime(b, (2, 2))
    assert [row["order"] for row in logs.read_recent(project, last=2)] == [2, 3]


@pytest.mark.parametrize("changes", [
    {"logs": {"max_file_bytes": 5000, "max_total_bytes": 1024}},
    {"logs": {"keep_transcripts": "no"}},
    {"ui": {"toggle_bar": "invalid"}},
    {"mode": "unsafe"},
    {"stt": {"backend": "cloud"}},
    {"audio": {"silence_seconds": 0.1, "short_silence_seconds": 0.25}},
])
def test_invalid_reload_preserves_current_generation_and_recovers(project, changes):
    reloader = ConfigReloader(project)
    old = reloader.loaded
    settings(project, **changes)
    result = reloader.poll()
    assert result is not None and not result.ok
    assert reloader.loaded is old
    assert result.loaded is None
    assert reloader.poll() is None
    ensure_default_config(project, portable=True)
    result = reloader.poll()
    assert result is not None and result.ok


def test_alias_reload_publishes_only_complete_valid_snapshot(project):
    reloader = ConfigReloader(project)
    assert reloader.poll() is None
    (project / "config/aliases.yaml").write_text("editor: Notepad\n", encoding="utf-8")
    result = reloader.poll()
    assert result.ok and result.changed_files == ("aliases.yaml",)
    assert result.loaded.catalog.aliases["editor"] == "Notepad"
    assert result.executor_settings["window_timeout_seconds"] == 30
    assert reloader.poll() is None


def test_doctor_reports_each_failure_without_skipping_other_checks(project, capsys):
    def bad_microphone():
        raise RuntimeError("No default input device")

    checks = run_doctor(project, microphone_check=bad_microphone,
                        model_check=lambda *args: "models present",
                        hotkey_check=lambda *args: "registered",
                        resolver_check=lambda *args: "disabled")
    assert [check.name for check in checks] == ["microphone", "models", "hotkeys", "resolver"]
    assert [check.ok for check in checks] == [False, True, True, True]
    assert print_doctor(checks) is False
    output = capsys.readouterr().out
    assert "FIX microphone" in output and "allow desktop microphone access" in output
    assert "OK hotkeys" in output


def test_missing_models_never_download_and_disabled_resolver_never_connects(project, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("Disabled resolver must not connect")

    monkeypatch.setattr("voicebar.resolver.ollama.OllamaBackend.local_model", network_forbidden)
    checks = run_doctor(project, microphone_check=lambda: "test closed", hotkey_check=lambda root: "registered")
    assert checks[1].ok is False and "No speech backend" in checks[1].detail
    assert checks[3].ok is True and "disabled" in checks[3].detail
