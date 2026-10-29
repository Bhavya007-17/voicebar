"""Print honest before/after hop percentiles from local JSON decision logs."""

import argparse
import json
from pathlib import Path

from voicebar.latency import markdown_table, summarize_logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, nargs="+", required=True)
    parser.add_argument("--after", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    def paths(values):
        return [file for value in values for file in (
            sorted(value.glob("*.jsonl")) if value.is_dir() else [value])]

    before = summarize_logs(paths(args.before))
    after = summarize_logs(paths(args.after)) if args.after else None
    result = {"before": before, "after": after}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(markdown_table(before, after))
    if before["errors"] or after and after["errors"]:
        raise SystemExit("Some logs could not be read; inspect the saved error list")


if __name__ == "__main__":
    main()
