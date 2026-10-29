"""Prepare the whole parser contract as local System.Speech WAV diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from voicebar.catalog import load_catalog
from voicebar.stt.corpus import (
    CorpusError,
    build_synthetic_manifest,
    save_manifest,
    validate_wav,
    write_empty_utterance,
)


def synthesize(project_root: Path, catalog) -> dict:
    """Create data files only: no recognition, microphone, parser execution, or UI."""
    root = Path(project_root).resolve()
    output = root / "fixtures" / "synthetic"
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_synthetic_manifest(root, catalog)
    jobs = []
    for item in manifest["items"]:
        path = output / item["wav"]
        if item["text"]:
            jobs.append({"id": item["id"], "text": item["text"], "rate": item["rate"], "wav": str(path)})
        else:
            write_empty_utterance(path)
    request = output / "synthesis-request.json"
    request.write_text(json.dumps({
        "outputRoot": str(output), "voice": manifest["voice"],
        "sampleRate": 16000, "channels": 1, "sampleWidth": 2, "items": jobs,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # Only fixed argv and a JSON file cross the language boundary. Utterances
    # never become PowerShell expressions, scriptblocks, SSML, or shell input.
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-File",
             str(root / "scripts" / "synthesize.ps1"), "-RequestPath", str(request)],
            shell=False, capture_output=True, encoding="utf-8-sig", errors="strict", check=True,
        )
        summary = json.loads(result.stdout)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise CorpusError(f"System.Speech synthesis failed: {detail[-1000:]}") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"Cannot run native System.Speech synthesis: {error}") from error
    if summary != {"count": len(jobs), "voice": manifest["voice"]}:
        raise CorpusError("System.Speech returned an unexpected synthesis summary.")
    for item in manifest["items"]:
        item.update(validate_wav(output / item["wav"]))
    save_manifest(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        manifest = synthesize(args.project_root, load_catalog(args.project_root).catalog)
    except (CorpusError, ValueError, OSError) as error:
        parser.exit(1, f"Synthesis failed: {error}\n")
    print(json.dumps({
        "wav_count": len(manifest["items"]), "voice": manifest["voice"], "rates": manifest["rates"],
        "manifest": str(args.project_root / "fixtures" / "synthetic" / "manifest.json"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
