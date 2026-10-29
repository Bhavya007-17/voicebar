"""Explicit setup of the pinned portable local model server; never imported at runtime."""
from __future__ import annotations

import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "0.40.11"
SHA256 = "bda4a493ba8a7c24d57263cd843cd68cee84f44aed4299eeed532acad7eda12a"
URL = f"https://github.com/ollama/ollama/releases/download/v{VERSION}/ollama-windows-amd64.zip"


def setup(root):
    root = Path(root).resolve()
    models = root / "models"
    models.mkdir(exist_ok=True)
    archive = models / f"ollama-runtime-{VERSION}.zip"
    destination = models / f"ollama-runtime-{VERSION}"
    if not archive.exists():
        print(f"Downloading official portable Ollama {VERSION}...", flush=True)
        urllib.request.urlretrieve(URL, archive)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise RuntimeError("Portable model-server archive checksum does not match the release")
    if not destination.resolve().is_relative_to(models.resolve()) or destination.is_symlink():
        raise RuntimeError("Portable model-server destination must stay inside project models")
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            path = PurePosixPath(entry.filename.replace("\\", "/"))
            if (path.is_absolute() or ".." in path.parts or ":" in entry.filename
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError("Unexpected archive path or symbolic link")
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Archive target escapes the portable directory")
        package.extractall(destination)
    executable = destination / "ollama.exe"
    if not executable.is_file():
        raise RuntimeError("Portable archive has no Ollama executable")
    (destination / "voicebar-runtime.json").write_text(json.dumps({
        "version": VERSION, "source": URL, "archive_sha256": SHA256,
    }, indent=2), encoding="utf-8")
    print(f"Verified portable local server: {executable}", flush=True)


if __name__ == "__main__":
    setup(Path(__file__).resolve().parents[1])
