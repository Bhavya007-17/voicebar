"""Profile the configured local recognizer against recorded personal audio.

This tool never executes a plan or opens a microphone. A cold first decode is
reported separately from warmed repeats; profiling overhead is labeled.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import time
from pathlib import Path

from voicebar.catalog import _yaml_file, load_catalog
from voicebar.stt import load_backend
from voicebar.stt.base import read_wav, transcribe_segment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--startup-warmup", action="store_true",
                        help="Exercise the backend's explicit local startup warmup before input")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "docs/evidence/stage8-recognition-profile.json"
    settings = _yaml_file(root / "config/settings.yaml")
    print(json.dumps({"event": "profile_started", "pid": os.getpid(),
                      "backend": settings["stt"]["backend"]}), flush=True)
    loaded = load_catalog(root)
    selected = []
    for manifest in sorted((root / "fixtures/mine").glob("*/manifest.json")):
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if document.get("completed"):
            selected.extend(manifest.parent / row["selected_wav"] for row in document["prompts"]
                            if row.get("selected_wav"))
    selected = selected[:args.count]
    if not selected:
        raise SystemExit("No completed personal recordings are available")
    began = time.perf_counter()
    backend = load_backend(settings["stt"]["backend"], root, loaded.catalog,
                           device=settings["stt"]["device"])
    result = {"backend": backend.name, "device": backend.device,
              "load_ms": (time.perf_counter() - began) * 1000,
              "scope": "unpaced final-only decode; no endpoint, microphone, gate or execution",
              "rows": []}
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    save()
    try:
        if args.startup_warmup:
            warmup = getattr(backend, "warmup", None)
            if warmup is None:
                raise RuntimeError("The selected backend has no explicit startup warmup")
            began = time.perf_counter()
            warmup()
            result["startup_warmup_ms"] = (time.perf_counter() - began) * 1000
            save()
            print(json.dumps({"event": "startup_warmup_complete",
                              "milliseconds": result["startup_warmup_ms"]}), flush=True)
        for round_number in range(2):
            for path in selected:
                events = []
                began = time.perf_counter()
                transcribe_segment(backend, read_wav(path), events.append)
                cost = (time.perf_counter() - began) * 1000
                row = {"wav": str(path.relative_to(root)), "round": round_number,
                       "stt_ms": cost, "text": " ".join(e.text for e in events if e.final)}
                result["rows"].append(row)
                save()
                print(json.dumps(row), flush=True)
        profile = cProfile.Profile()
        profile.enable()
        transcribe_segment(backend, read_wav(selected[0]), lambda event: None)
        profile.disable()
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats("cumulative").print_stats(45)
        result["profile_with_overhead"] = stream.getvalue()
        result["placement"] = getattr(backend, "placement", {})
        result["status"] = "complete"
    except BaseException as error:
        result["status"] = "interrupted_or_failed"
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        backend.close()
        save()
    print(result["profile_with_overhead"], flush=True)


if __name__ == "__main__":
    main()
