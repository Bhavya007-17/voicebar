"""Run a cold-load recognition proof without changing machine networking."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicebar.stt.offline import prove


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)

    def progress(done, total):
        if done % 20 == 0 or done == total:
            print(f"{args.backend}: {done}/{total} WAVs checked offline", file=sys.stderr, flush=True)

    report = prove(args.root, args.backend, args.manifest, device=args.device,
                   interval_ms=args.interval_ms, timeout=args.timeout, progress=progress)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("backend", "device", "ok", "returncode", "sample_count",
                                           "max_sample_gap_ms", "monitoring_error")}
    summary["evidence"] = str(args.output.resolve())
    summary["physical_network_disabled"] = False
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
