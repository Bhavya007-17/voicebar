"""Owned Calculator replay with an explicit experimental GPU layer count.

Uses the unchanged Stage 11 native probe, including its execution guard and
five-second vision deadline. Never changes production options or saves pixels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import ClassVar

import probe_stage11_desktop as desktop

from voicebar.resolver.ollama import OllamaBackend
from voicebar.resolver.vision import VisionSelector

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "scripts/probe_stage11_partial_vision.py", "scripts/probe_stage11_desktop.py",
    "scripts/probe_stage10.py", "voicebar/resolver/vision.py", "voicebar/resolver/ollama.py",
    "voicebar/screen.py", "voicebar/parser.py", "voicebar/executor/app_actions.py",
    "voicebar/listening.py", "voicebar/conversation.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-layers", type=int, choices=(12, 16), default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    native_output = output.with_name(f"{output.stem}-desktop.json")
    if output.exists() or native_output.exists():
        raise RuntimeError("Refusing to overwrite existing probe evidence")
    source_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    report = {"verified": False, "sources": source_hashes, "vision_calls": [],
              "screenshots_persisted": False, "physical_microphone_tested": False,
              "source_references": [
                  "https://github.com/ollama/ollama/blob/v0.40.11/api/types.go#L548-L557",
                  "https://github.com/ollama/ollama/blob/v0.40.11/llm/llama_server.go#L398-L406",
                  "https://github.com/ollama/ollama/blob/v0.40.11/llm/llama_server.go#L661-L667"],
              "limitation": "Partial text offload also puts the image projector on CPU in Ollama 0.40.11"}

    class PartialVisionSelector(VisionSelector):
        _OPTIONS: ClassVar[dict[str, int]] = {**VisionSelector._OPTIONS, "num_gpu": args.gpu_layers}

        def select(self, text, image_png, candidates, timeout_s=5):
            began = time.perf_counter()
            selected = super().select(text, image_png, candidates, timeout_s=timeout_s)
            report["vision_calls"].append({
                "text": text, "candidate_count": len(candidates), "selected_id": selected,
                "selected_control": candidates[selected - 1] if selected is not None else None,
                "timeout_s": timeout_s, "elapsed_ms": (time.perf_counter() - began) * 1000,
                "outcome": self.last_outcome,
            })
            return selected

    report["options"] = dict(PartialVisionSelector._OPTIONS)
    backend = OllamaBackend("qwen3-vl:2b-instruct")
    report["server_version"] = backend._request("/api/version")
    if report["server_version"] != {"version": "0.40.11"}:
        raise RuntimeError("Server version differs from the verified option documentation")
    report["resident_before"] = backend._request("/api/ps")
    if report["resident_before"].get("models"):
        raise RuntimeError("Another model is resident; coordinate its owner before benchmarking")
    info = backend.local_model()
    report["model_info"] = {key: value for key, value in info.get("model_info", {}).items()
                            if any(part in key for part in ("block_count", "architecture", "parameter_count"))}
    if report["model_info"].get("qwen3vl.block_count") != 28:
        raise RuntimeError("Model layer count differs from the verified candidate")
    original_selector, original_argv = desktop.VisionSelector, sys.argv
    try:
        desktop.VisionSelector = PartialVisionSelector
        sys.argv = [str(ROOT / "scripts/probe_stage11_desktop.py"), "--output", str(native_output)]
        report["native_exit_code"] = desktop.main()
        report["native_result"] = json.loads(native_output.read_text(encoding="utf-8"))
        report["resident_after"] = backend._request("/api/ps")
        report["verified"] = report["native_exit_code"] == 0 and report["native_result"]["verified"]
        if source_hashes != {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}:
            raise RuntimeError("Probe or production source changed during execution")
    except (OSError, RuntimeError, ValueError) as error:
        report["verified"] = False
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        desktop.VisionSelector, sys.argv = original_selector, original_argv
        try:
            backend.unload()
            report["resident_after_unload"] = backend._request("/api/ps")
        except (OSError, RuntimeError, ValueError) as error:
            report["unload_error"] = f"{type(error).__name__}: {error}"
            report["verified"] = False
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
