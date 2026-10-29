"""Monitor an owned Voicebar app playing WAVs; never changes networking."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voicebar.network_proof import prove


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backend", default="qwen")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--expect-offline", action="store_true",
                        help="Require all physical adapters disabled/disconnected before and after")
    parser.add_argument("--headless", action="store_true", help="Exercise controller without a Qt bar")
    args = parser.parse_args(argv)
    report = prove(args.root, args.manifest, backend=args.backend, device=args.device,
                   interval_ms=args.interval_ms, timeout=args.timeout,
                   expect_offline=args.expect_offline, gui=not args.headless)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    summary = {key: report[key] for key in (
        "ok", "backend", "device", "returncode", "sample_count", "max_sample_gap_ms",
        "physical_network_disabled", "monitoring_error", "manifest_sha256")}
    summary["output"] = str(args.output.resolve())
    print(json.dumps(summary, indent=2))
    executable = str((args.root / ".venv/Scripts/python.exe").resolve()).replace("'", "''")
    print("Optional admin command (printed only):")
    print("New-NetFirewallRule -DisplayName 'Voicebar block outbound' -Direction Outbound "
          f"-Program '{executable}' -Action Block -Profile Any")
    runtime_image = report.get("worker_image")
    if runtime_image and Path(runtime_image) != Path(executable):
        print("The venv launcher uses a shared interpreter. Its image also needs a rule; "
              "this affects other projects using that interpreter:")
        for candidate in (Path(runtime_image), Path(runtime_image).with_name("pythonw.exe")):
            if candidate.is_file():
                escaped = str(candidate).replace("'", "''")
                print("New-NetFirewallRule -DisplayName 'Voicebar shared interpreter "
                      f"{candidate.name}' -Direction Outbound -Program '{escaped}' -Action Block -Profile Any")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
