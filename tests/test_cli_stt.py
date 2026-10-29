"""CLI stage boundaries never enable unmeasured speech execution."""

from types import SimpleNamespace

import pytest

from voicebar import cli


@pytest.fixture
def speech_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("stt:\n  backend: null\nptt:\n  hotkey: ctrl+alt+f8\n", encoding="utf-8")
    loaded = SimpleNamespace(catalog=object())
    monkeypatch.setattr(cli, "load_catalog", lambda *args, **kwargs: loaded)
    return tmp_path, loaded


def test_unselected_live_backend_refuses_before_ptt(speech_root, monkeypatch, capsys):
    root, _ = speech_root
    monkeypatch.setattr("voicebar.ptt.listen_ptt", lambda *a, **k: pytest.fail("Must refuse before capture"))
    assert cli.main(["listen", "--ptt", "--backend", "parakeet"], project_root=root) == 2
    assert "accuracy gate is pending" in capsys.readouterr().err


def test_unconfigured_always_on_requires_explicit_candidate(speech_root, capsys):
    root, _ = speech_root
    assert cli.main(["listen", "--dry-run"], project_root=root) == 2
    assert "explicit --backend" in capsys.readouterr().err


def test_unselected_dry_run_requires_explicit_candidate_before_capture(speech_root, monkeypatch, capsys):
    root, _ = speech_root
    monkeypatch.setattr("voicebar.ptt.listen_ptt", lambda *a, **k: pytest.fail("Must refuse before capture"))
    assert cli.main(["listen", "--ptt", "--dry-run"], project_root=root) == 2
    assert "explicit --backend" in capsys.readouterr().err


def test_dry_run_candidate_and_configured_hotkey_reach_ptt(speech_root, monkeypatch):
    root, loaded = speech_root
    calls = []
    monkeypatch.setattr("voicebar.ptt.listen_ptt", lambda *a, **k: calls.append((a, k)))
    assert cli.main(["listen", "--ptt", "--dry-run", "--backend", "parakeet"], project_root=root) == 0
    assert calls == [((root, loaded), {"backend_name": "parakeet", "device": "cpu", "hotkey": "ctrl+alt+f8", "dry_run": True})]


@pytest.mark.parametrize("settings", ["stt: bad\n", "stt:\n  backend: unknown\n", "stt:\n  device: other\n"])
def test_invalid_speech_settings_are_plain_errors(speech_root, settings, capsys):
    root, _ = speech_root
    (root / "config/settings.yaml").write_text(settings, encoding="utf-8")
    assert cli.main(["listen", "--ptt", "--dry-run"], project_root=root) == 2
    assert capsys.readouterr().err.startswith("voicebar:")
