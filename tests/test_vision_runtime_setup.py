"""Portable setup validates the pinned archive before extracting or selecting it."""
import hashlib
import json
import zipfile

import pytest

from scripts import setup_vision_runtime as runtime


def archive(tmp_path, entries):
    models = tmp_path / "models"
    models.mkdir()
    path = models / f"ollama-runtime-{runtime.VERSION}.zip"
    with zipfile.ZipFile(path, "w") as package:
        for name, data in entries.items():
            package.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verified_archive_creates_project_scoped_portable_runtime(tmp_path, monkeypatch):
    digest = archive(tmp_path, {"ollama.exe": b"owned test fixture"})
    monkeypatch.setattr(runtime, "SHA256", digest)
    runtime.setup(tmp_path)
    destination = tmp_path / f"models/ollama-runtime-{runtime.VERSION}"
    assert (destination / "ollama.exe").read_bytes() == b"owned test fixture"
    assert json.loads((destination / "voicebar-runtime.json").read_text())["archive_sha256"] == digest


def test_mismatched_archive_never_extracts(tmp_path):
    archive(tmp_path, {"ollama.exe": b"wrong checksum"})
    with pytest.raises(RuntimeError, match="checksum"):
        runtime.setup(tmp_path)
    assert not (tmp_path / f"models/ollama-runtime-{runtime.VERSION}").exists()


@pytest.mark.parametrize("name", ["../escape.exe", "/absolute.exe", "C:/escape.exe", "..\\escape.exe"])
def test_archive_paths_cannot_escape_destination(tmp_path, monkeypatch, name):
    digest = archive(tmp_path, {name: b"not extracted"})
    monkeypatch.setattr(runtime, "SHA256", digest)
    with pytest.raises(RuntimeError, match="archive path"):
        runtime.setup(tmp_path)
    assert not (tmp_path / f"models/ollama-runtime-{runtime.VERSION}").exists()
