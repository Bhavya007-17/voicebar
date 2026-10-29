"""Benchmark saved local fixtures without audio capture or action execution."""

import argparse
import json
from pathlib import Path

from voicebar.stt import BACKEND_NAMES
from voicebar.stt.benchmark import BenchmarkError, run_benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backend", choices=BACKEND_NAMES, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--subset", choices=("mine", "synthetic"), default="mine")
    parser.add_argument("--session", action="append", dest="sessions", metavar="SESSION_DIRECTORY_NAME",
                        help="Select an exact complete mine session directory; repeat to include more sessions")
    parser.add_argument("--timing", choices=("batch", "realtime"), default="realtime")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--proof", type=Path, help="Reuse verified synthetic offline proof transcripts for batch accuracy")
    parser.add_argument("--limit", type=int, help="Process this many pending items, preserving a resumable checkpoint")
    args = parser.parse_args()

    def progress(done, total, row):
        print(json.dumps({"done": done, "total": total, "id": row["id"],
                          "exact_plan": row.get("exact_plan"), "text": row.get("text"),
                          "last_speech_to_final_ms": row.get("last_speech_to_final_ms")}, ensure_ascii=True), flush=True)

    try:
        report = run_benchmark(args.root, args.backend, device=args.device, subset=args.subset,
                               timing=args.timing, output=args.output, resume=args.resume,
                               proof=args.proof, limit=args.limit, progress=progress, sessions=args.sessions)
    except (BenchmarkError, RuntimeError, OSError, ValueError) as error:
        parser.exit(1, f"Benchmark failed: {error}\n")
    print(json.dumps({"output": str(args.output), "completed": report["completed"],
                      "summary": report["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
