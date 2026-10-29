"""Evaluate the installed vision model as the existing closed text selector.

Reuses the unchanged Stage 6 benchmark and reviewed 80-fixture gate. No action
execution, model downloads, runtime configuration edits, or invented payloads.
The sole inference-option change is a per-instance 4096-token context, matching
the existing vision selector so the two paths can share one resident runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts import benchmark_stage6 as stage6
from voicebar.resolver.ollama import OllamaBackend
from voicebar.resolver.vision import VisionSelector

MODEL = "qwen3-vl:2b-instruct"
SOURCES = (
    "scripts/probe_stage11_shared_selector.py", "scripts/benchmark_stage6.py",
    "voicebar/resolver/ollama.py", "voicebar/resolver/vision.py",
    "voicebar/resolver/base.py", "voicebar/resolver/candidates.py",
    "voicebar/resolver/eligibility.py", "voicebar/parser.py",
    "voicebar/normalization.py", "voicebar/gate.py", "voicebar/catalog.py",
)


def hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.exists():
        raise stage6.BenchmarkError("Preserve existing evidence; choose a new output path")
    context = VisionSelector._OPTIONS["num_ctx"]
    if context != 4096:
        raise stage6.BenchmarkError("The proposed shared context no longer matches vision")
    options = {**OllamaBackend._OPTIONS, "num_ctx": context}
    source_hashes = hashes(root)
    environment = {
        "scope": "Only the already installed vision model; complete unchanged Stage 6 gate",
        "model": MODEL, "text_options": options,
        "vision_options": dict(VisionSelector._OPTIONS),
        "text_request_path": "/api/generate", "think": False, "keep_alive": -1,
        "server_version": stage6._local_request("/api/version"),
        "resident_before": stage6._local_request("/api/ps"),
        "source_sha256": source_hashes,
        "configuration_changed": False, "actions_executed": False,
    }

    def request(path, body=None, *, timeout_s=10.0):
        result = stage6._local_request(path, body, timeout_s=timeout_s)
        if path == "/api/tags":
            if not isinstance(result.get("models"), list):
                raise stage6.BenchmarkError("Invalid model inventory")
            environment["inventory_names"] = [entry.get("name") for entry in result["models"]]
            result = {**result, "models": [entry for entry in result["models"]
                                          if entry.get("name") == MODEL]}
            if len(result["models"]) != 1:
                raise stage6.BenchmarkError("The explicitly installed vision model is unavailable")
        return result

    def backend_factory(model):
        if model != MODEL:
            raise stage6.BenchmarkError("Unexpected model entered the scoped benchmark")
        backend = OllamaBackend(model)
        backend._OPTIONS = dict(options)
        return backend

    def progress(model, count, total, row):
        if count == total:
            environment["resident_after_last_fixture_before_unload"] = stage6._local_request("/api/ps")
        if count % 10 == 0 or count == total or row["outcome"] not in {"resolved", "not_sure"}:
            print(f"{model}: {count}/{total} {row['outcome']} "
                  f"({row['elapsed_ms']:.1f} ms)", flush=True)

    report = stage6.run_benchmark(root, args.output, request=request,
                                 backend_factory=backend_factory, progress=progress)
    environment["sources_unchanged"] = hashes(root) == source_hashes
    environment["resident_after_benchmark_unload"] = stage6._local_request("/api/ps")
    report["shared_selector_proposal"] = environment
    if not environment["sources_unchanged"]:
        for result in report["models"]:
            result["eligible"] = False
        report["chosen_model"] = None
        report["error"] = "Source changed during the benchmark; evidence cannot authorize configuration"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(stage6.table(report), flush=True)
    return 0 if report["chosen_model"] == MODEL else 2


if __name__ == "__main__":
    raise SystemExit(main())
