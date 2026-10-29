"""Transcribe explicitly recorded Stage 4 wake clips locally, without actions.

The input must be a direct child of fixtures/mine. Clips may contain incidental
speech or segmentation splits; filename order never assigns expected prompts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from voicebar.catalog import load_catalog
from voicebar.listening import configured_gate
from voicebar.parser import parse, tokenize
from voicebar.stt import load_backend
from voicebar.stt.base import transcribe_wav
from voicebar.stt.corpus import validate_wav
from voicebar.stt.offline import SocketAuditPolicy


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recording_directory(root: Path, supplied: Path) -> Path:
    directory = (supplied if supplied.is_absolute() else root / supplied).resolve()
    mine = (root / "fixtures" / "mine").resolve()
    if directory.parent != mine or not directory.is_dir():
        raise ValueError("--directory must name an existing direct child of fixtures/mine")
    return directory


def wake_observation(text: str, catalog, names: set[str]) -> dict:
    tokens = tokenize(text)
    index = 0
    while index < len(tokens) and tokens[index].norm in {"hey", "ok", "okay"}:
        index += 1
    leading = tokens[index].norm if index < len(tokens) else None
    named = leading in names
    remainder = " ".join(token.raw for token in tokens[index + 1:]) if named else None
    return {
        "leading_token": leading,
        "matched_configured_name": named,
        "exact_name": leading if named else None,
        "command_tail": remainder,
        "command_tail_plan": parse(remainder, catalog) if remainder else None,
    }


def transcribe(root: Path, directory: Path) -> Path:
    loaded = load_catalog(root)
    names = configured_gate(root, loaded).names
    paths = sorted(directory.glob("wake-*.wav"))
    if not paths:
        raise ValueError("No wake-*.wav recordings exist in --directory")
    for path in paths:
        if path.resolve().parent != directory or not path.is_file():
            raise ValueError("Every recorded clip must be a regular file within --directory")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = root / "docs/evidence" / f"stage4-personal-wake-{stamp}-{uuid.uuid4().hex[:8]}"
    output.mkdir(parents=True)
    metadata = {
        "source_directory": str(directory.relative_to(root)),
        "backend": "qwen", "device": "cuda", "complete": False,
        "configured_names": sorted(names),
        "inputs": {str(path.relative_to(root)): {"sha256": digest(path), **validate_wav(path)}
                   for path in paths},
        "source_sha256": {relative: digest(root / relative) for relative in (
            "scripts/transcribe_stage4_wake.py", "voicebar/stt/qwen.py",
            "voicebar/stt/base.py", "voicebar/parser.py", "voicebar/gate.py",
            "voicebar/stt/context.py", "models/manifest.json",
        )},
        "limitations": [
            "Clips may include incidental speech or segmentation splits; no filename is assigned an expected prompt.",
            "This records actual wake-name outputs, not a measured recognition accuracy rate.",
            "No actions execute and no live gate state is simulated.",
            "Python socket refusal does not establish native network isolation or physical networking off.",
        ],
    }
    run = output / "run.json"
    run.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    policy = SocketAuditPolicy()
    policy.install_and_check()
    backend = None
    rows = []
    try:
        backend = load_backend("qwen", root, loaded.catalog, device="cuda")
        with (output / "transcripts.jsonl").open("w", encoding="utf-8") as stream:
            for path in paths:
                source = str(path.relative_to(root))
                if digest(path) != metadata["inputs"][source]["sha256"]:
                    raise ValueError(f"Recorded input changed before transcription: {source}")
                row = {"source": source, **transcribe_wav(backend, path)}
                if digest(path) != metadata["inputs"][source]["sha256"]:
                    raise ValueError(f"Recorded input changed during transcription: {source}")
                row["wake_observation"] = wake_observation(row["text"], loaded.catalog, names)
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                print(json.dumps({"source": source, "text": row["text"],
                                  "wake_observation": row["wake_observation"]}), flush=True)
        runtime_events = [event for event in policy.events if event["phase"] == "runtime"]
        if runtime_events:
            raise RuntimeError("Runtime attempted network use; personal evidence run fails")
        metadata.update({"complete": True, "transcribed_clips": len(rows),
                         "python_socket_attempts": runtime_events,
                         "runtime_details": dict(backend.placement),
                         "observed_exact_names": sorted({row["wake_observation"]["exact_name"]
                                                         for row in rows
                                                         if row["wake_observation"]["exact_name"]})})
        run.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return output
    except BaseException as error:
        (output / "failure.json").write_text(json.dumps({
            "type": type(error).__name__, "error": str(error), "transcribed_clips": len(rows),
            "python_socket_events": policy.events,
        }, indent=2) + "\n", encoding="utf-8")
        raise
    finally:
        if backend is not None:
            backend.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.project_root.resolve()
    directory = recording_directory(root, args.directory)
    output = transcribe(root, directory)
    print(json.dumps({"evidence_directory": str(output), "complete": True}, indent=2))


if __name__ == "__main__":
    main()
