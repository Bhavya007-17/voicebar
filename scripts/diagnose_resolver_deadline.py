"""Observe unchanged 20 ms resolver deadlines using an in-process fake backend.

This is a diagnostic, not a gate replacement. It never loads a model, opens a
microphone, creates a window, accesses networking or changes the deadline.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from pathlib import Path

from voicebar.catalog import Catalog
from voicebar.resolver import base


def summarize(values):
    ordered = sorted(values)
    return {"p50_ms": statistics.median(ordered),
            "p95_ms": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
            "max_ms": max(ordered)} if ordered else None


def diagnose(count):
    catalog = Catalog(
        apps=["Google Chrome", "Autodesk Fusion", "Notepad", "Calculator"],
        aliases={"browser": "Google Chrome", "fusion": "Autodesk Fusion"},
        sites={"youtube": "https://www.youtube.com", "github": "https://github.com"},
        engines={"google": "https://www.google.com/search?q={q}",
                 "youtube": "https://www.youtube.com/results?search_query={q}"})
    text = "computer make the current window take up all the space"
    action = {"action": "window", "op": "maximize", "target": "active"}
    original_thread = threading.Thread
    originals = {name: getattr(base, name)
                 for name in ("parse", "eligible_request", "build_request", "decode_selection")}
    active = {}

    class ObservedThread(original_thread):
        def start(self):
            self.observed = active
            self.observed["thread_start_requested"] = time.perf_counter_ns()
            super().start()

        def run(self):
            self.observed["worker_entered"] = time.perf_counter_ns()
            self.observed["worker_cpu_entered"] = time.thread_time_ns()
            try:
                super().run()
            finally:
                self.observed["worker_exited"] = time.perf_counter_ns()
                self.observed["worker_cpu_exited"] = time.thread_time_ns()

    def observe(name, function):
        def measured(*args, **kwargs):
            trace = active
            wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
            try:
                return function(*args, **kwargs)
            finally:
                trace.setdefault("phases", {})[name] = {
                    "wall_ms": (time.perf_counter_ns() - wall) / 1e6,
                    "cpu_ms": (time.thread_time_ns() - cpu) / 1e6,
                }
        return measured

    attempts = []
    try:
        threading.Thread = ObservedThread
        for name, function in originals.items():
            setattr(base, name, observe(name, function))
        for number in range(count):
            entered, release = threading.Event(), threading.Event()

            class Delayed:
                def __init__(self, entered_event, release_event):
                    self.entered = entered_event
                    self.release = release_event

                def select(self, request, timeout_s):
                    self.entered.set()
                    self.release.wait()
                    identifier = next(candidate.id for candidate in request.candidates
                                      if candidate.action == action)
                    return json.dumps({"actions": [identifier]})

            resolver = base.Resolver(Delayed(entered, release), timeout_ms=20)
            active = {}
            initial = resolver.resolve(text, catalog)
            initial_outcome = resolver.last_outcome
            initial_entered = entered.is_set()
            release.set()
            if not resolver.wait_idle(1):
                raise RuntimeError("Owned fake worker did not finish after release")
            active = {}
            trace = active
            wall = time.perf_counter_ns()
            result = resolver.resolve(text, catalog)
            elapsed = (time.perf_counter_ns() - wall) / 1e6
            outcome = resolver.last_outcome
            if not resolver.wait_idle(1):
                raise RuntimeError("Owned second fake worker did not finish")
            # wait_idle signals before the wrapper's final bookkeeping. Joining
            # this worker observes completion without changing resolve's result.
            for worker in threading.enumerate():
                if isinstance(worker, ObservedThread) and worker.observed is trace:
                    worker.join(1)
            start = trace.get("thread_start_requested")
            worker_start, worker_end = trace.get("worker_entered"), trace.get("worker_exited")
            cpu_start, cpu_end = trace.get("worker_cpu_entered"), trace.get("worker_cpu_exited")
            attempts.append({
                "number": number + 1, "initial_none": initial is None,
                "initial_outcome": initial_outcome, "initial_backend_entered": initial_entered,
                "second_resolved": result == [action], "second_outcome": outcome,
                "second_elapsed_ms": elapsed,
                "worker_start_delay_ms": (worker_start - start) / 1e6 if worker_start and start else None,
                "worker_wall_ms": (worker_end - worker_start) / 1e6 if worker_end and worker_start else None,
                "worker_cpu_ms": (cpu_end - cpu_start) / 1e6
                    if cpu_end is not None and cpu_start is not None else None,
                "phases": trace.get("phases", {}),
            })
    finally:
        threading.Thread = original_thread
        for name, function in originals.items():
            setattr(base, name, function)
    return {
        "purpose": "Unchanged 20 ms resolver behavior under local diagnostic instrumentation",
        "test_replacement": False, "deadline_ms": 20, "iterations": count,
        "clock_info": {name: {"implementation": time.get_clock_info(name).implementation,
                              "resolution_seconds": time.get_clock_info(name).resolution}
                       for name in ("monotonic", "perf_counter", "thread_time")},
        "second_resolved": sum(row["second_resolved"] for row in attempts),
        "second_outcomes": {outcome: sum(row["second_outcome"] == outcome for row in attempts)
                            for outcome in sorted({row["second_outcome"] for row in attempts})},
        "second_elapsed": summarize([row["second_elapsed_ms"] for row in attempts]),
        "worker_start_delay": summarize([row["worker_start_delay_ms"] for row in attempts
                                          if row["worker_start_delay_ms"] is not None]),
        "worker_wall": summarize([row["worker_wall_ms"] for row in attempts if row["worker_wall_ms"] is not None]),
        "worker_cpu": summarize([row["worker_cpu_ms"] for row in attempts if row["worker_cpu_ms"] is not None]),
        "attempts": attempts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 1000:
        parser.error("--count must be from 1 to 1000")
    report = diagnose(args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "attempts"}, indent=2))
