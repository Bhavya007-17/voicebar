"""Test 20 GPU layers for the text selector with the unchanged Stage 6 gate.

All 80 reviewed fixtures, closed candidates, 8192-token context, and 1500 ms
deadline are preserved. No action execution or production option changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from scripts import benchmark_stage6 as stage6
from scripts.probe_stage11_cpu_selector import OPTION_SOURCES, SERVER_VERSION, SOURCES
from voicebar.resolver.ollama import OllamaBackend

MODEL = "qwen2.5:3b"
GPU_LAYERS = 20
PROBE_SOURCES = (*SOURCES, "scripts/probe_stage11_partial_selector.py")


def hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in PROBE_SOURCES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.exists():
        raise stage6.BenchmarkError("Preserve existing evidence; choose a new output path")
    settings = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8-sig"))
    if settings.get("resolver", {}).get("model") != MODEL:
        raise stage6.BenchmarkError("The configured selector differs from this proposal")
    if OllamaBackend._OPTIONS != {"temperature": 0, "num_predict": 96, "num_ctx": 8192}:
        raise stage6.BenchmarkError("Production selector options changed; review this proposal again")
    version = stage6._local_request("/api/version")
    if version.get("version") != SERVER_VERSION:
        raise stage6.BenchmarkError("Verify num_gpu against the current server source before running")
    resident_before = stage6._local_request("/api/ps")
    if resident_before.get("models") != []:
        raise stage6.BenchmarkError("This isolated comparison requires no resident Ollama models")
    info = OllamaBackend(MODEL).local_model()
    model_info = {key: value for key, value in info.get("model_info", {}).items()
                  if any(part in key for part in ("block_count", "architecture", "parameter_count"))}
    if model_info.get("qwen2.block_count") != 36:
        raise stage6.BenchmarkError("Model layer count differs from the verified candidate")
    options = {**OllamaBackend._OPTIONS, "num_gpu": GPU_LAYERS}
    source_hashes = hashes(root)
    environment = {
        "scope": "20 GPU text layers; complete unchanged reviewed Stage 6 gate",
        "model": MODEL, "model_info": model_info, "text_options": options,
        "text_request_path": "/api/generate", "think": False, "keep_alive": -1,
        "option_sources": OPTION_SOURCES, "server_version": version,
        "resident_before": resident_before, "source_sha256": source_hashes,
        "configuration_changed": False, "actions_executed": False,
        "limitations": ["One pass; no concurrent speech or vision inference",
                        "Partial model placement alone does not establish whole-app resource fit"],
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
                raise stage6.BenchmarkError("The configured local text model is unavailable")
        return result

    class PartialBackend(OllamaBackend):
        def warm(self, timeout_s=60.0):
            warmed = super().warm(timeout_s)
            resident = stage6._local_request("/api/ps")
            environment["resident_after_warm"] = resident
            rows = resident.get("models", [])
            if (len(rows) != 1 or rows[0].get("name") != MODEL
                    or not 0 < rows[0].get("size_vram", 0) < rows[0].get("size", 0)):
                raise stage6.BenchmarkError("Partial GPU model residency was not confirmed")
            return warmed

    def backend_factory(model):
        if model != MODEL:
            raise stage6.BenchmarkError("Unexpected model entered the scoped comparison")
        backend = PartialBackend(model)
        backend._OPTIONS = dict(options)
        return backend

    def progress(model, count, total, row):
        if count == total:
            environment["resident_after_last_fixture_before_unload"] = stage6._local_request("/api/ps")
        if count % 10 == 0 or count == total or row["outcome"] not in {"resolved", "not_sure"}:
            print(f"{model} GPU layers {GPU_LAYERS}: {count}/{total} {row['outcome']} "
                  f"({row['elapsed_ms']:.1f} ms)", flush=True)

    try:
        report = stage6.run_benchmark(root, args.output, request=request,
                                     backend_factory=backend_factory, progress=progress)
    finally:
        resident = stage6._local_request("/api/ps")
        if any(row.get("name") == MODEL for row in resident.get("models", [])):
            OllamaBackend(MODEL).unload()
    environment["sources_unchanged"] = hashes(root) == source_hashes
    environment["resident_after_benchmark_unload"] = stage6._local_request("/api/ps")
    report["partial_selector_proposal"] = environment
    if not environment["sources_unchanged"]:
        for result in report["models"]:
            result["eligible"] = False
        report["chosen_model"] = None
        report["error"] = "Source changed during benchmark; evidence cannot authorize configuration"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(stage6.table(report), flush=True)
    return 0 if report["chosen_model"] == MODEL else 2


if __name__ == "__main__":
    raise SystemExit(main())
