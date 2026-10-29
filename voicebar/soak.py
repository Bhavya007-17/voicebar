"""Durable soak outcomes; an empty partial run is never a passing soak."""

import copy
import json
import os
import uuid
from pathlib import Path


def save_report(session, log_path, minutes):
    from .logs import cap_logs, load_log_settings, private_decision
    path = Path(log_path).with_suffix(".report.json")
    settings = load_log_settings(path.parent.parent)
    outcome = copy.deepcopy(getattr(session, "outcome", None) or {
        "complete": False, "status": "unknown", "stop_reason": "missing_outcome",
    })
    triggers = copy.deepcopy(outcome.get(
        "would_run", getattr(session, "report_would_run", session.would_run)))
    report = {
        **outcome, "mode": session.gate.mode, "minutes": minutes,
        "would_run": triggers, "log": str(log_path), "executor": "dry-run",
        "decision_count": outcome.get("decision_count", getattr(session, "decision_count", None)),
        "reason_counts": copy.deepcopy(outcome.get("reason_counts", getattr(session, "reason_counts", {}))),
    }
    if not settings["keep_transcripts"]:
        report = {**private_decision(report), "transcripts_stored": False}
    if len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > settings["max_total_bytes"]:
        raise OSError("Soak report exceeds the configured total log size; decisions remain in the rotated log")
    # Replace only after a complete JSON file is flushed on this same volume.
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        cap_logs(path.parent, settings["max_total_bytes"], active=path)
    finally:
        temporary.unlink(missing_ok=True)
    return report, path


def print_report(report, path):
    print("SOAK REPORT" if report["complete"] else "INCOMPLETE SOAK REPORT")
    for row in report["would_run"]:
        print(json.dumps(row, ensure_ascii=False))
    if report["complete"]:
        print(f"{len(report['would_run'])} utterances would run. Report: {path}")
    else:
        print(f"{len(report['would_run'])} processed utterances would run; "
              f"the requested soak did not complete. Report: {path}")
