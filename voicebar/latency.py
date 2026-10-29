"""Summarize measured JSON-log hops without substituting completion for start."""

from __future__ import annotations

import json
import math
from pathlib import Path

HOPS = {
    "end_of_speech_wait": "endpoint_ms",
    "recognition": "stt_ms",
    "gate_and_parse": "gate_ms",
    "execute_start": "execute_start_ms",
}


def percentiles(values):
    ordered = sorted(number for number in values
                     if not isinstance(number, bool) and isinstance(number, (int, float))
                     and math.isfinite(number) and number >= 0)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None}
    return {"count": len(ordered),
            "p50_ms": ordered[math.ceil(.5 * len(ordered)) - 1],
            "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1]}


def summarize_logs(paths):
    rows, errors = [], []
    for path in sorted(map(Path, paths)):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as error:
            errors.append({"path": str(path), "error": str(error)})
            continue
        for line_number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except ValueError as error:
                errors.append({"path": str(path), "line": line_number, "error": str(error)})
                continue
            if isinstance(row, dict) and row.get("event") == "decision":
                rows.append(row)
    timings = [row.get("timings", {}) for row in rows if isinstance(row.get("timings"), dict)]
    return {"decision_rows": len(rows), "hops": {
        hop: {"field": field, **percentiles(timing.get(field) for timing in timings)}
        for hop, field in HOPS.items()}, "errors": errors,
        "scope": "measured fields only; missing hops stay unavailable; execution completion is excluded"}


def markdown_table(before, after=None):
    header = ["| Hop | Before p50 / p95 (ms) | After p50 / p95 (ms) | Samples before / after |",
              "| --- | ---: | ---: | ---: |"]

    def display(hop):
        if not hop or not hop["count"]:
            return "unavailable"
        return f"{hop['p50_ms']:.2f} / {hop['p95_ms']:.2f}"

    for name in HOPS:
        first = before["hops"][name]
        second = after["hops"][name] if after else None
        header.append(f"| {name.replace('_', ' ')} | {display(first)} | {display(second)} | "
                      f"{first['count']} / {second['count'] if second else 0} |")
    return "\n".join(header)
