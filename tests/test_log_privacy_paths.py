"""Every utterance log path obeys the same privacy policy."""

import json
from types import SimpleNamespace

from voicebar.soak import save_report


def test_soak_report_removes_transcript_and_free_text_when_disabled(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("logs: {keep_transcripts: false}\n")
    (tmp_path / "logs").mkdir()
    secret = "private dictated sentence"
    trigger = {"text": secret, "reason": "run", "plan": [{"action": "type_text", "text": secret,
                                                         "submit": False}]}
    session = SimpleNamespace(gate=SimpleNamespace(mode="name"), would_run=[trigger],
                              outcome={"complete": True, "would_run": [trigger]})
    report, path = save_report(session, tmp_path / "logs/listen-owned.jsonl", 1)
    assert secret not in path.read_text(encoding="utf-8")
    assert report["would_run"][0]["reason"] == "run"
    assert report["would_run"][0]["plan"][0]["action"] == "type_text"
    assert json.loads(path.read_text())["transcripts_stored"] is False


def test_ptt_writer_uses_shared_bounded_private_logger(tmp_path):
    from test_ptt import FakeHotkeys, FakeMicrophone, run

    from voicebar.catalog import Catalog, InstalledApp, LoadedCatalog
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("logs: {keep_transcripts: false}\n")
    catalog = Catalog(apps=["Notepad", "Calculator"])
    loaded = LoadedCatalog(catalog, [InstalledApp(name, name) for name in catalog.apps], {}, [])
    controls = FakeHotkeys()
    result = run(tmp_path, loaded, controls=controls, mic=FakeMicrophone(controls))
    document = json.loads(result.report.log_path.read_text(encoding="utf-8"))
    assert document["transcripts_stored"] is False
    assert "text" not in document
