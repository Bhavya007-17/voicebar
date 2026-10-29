"""Compare text-linear NF4 Qwen ASR against the unchanged 40-recording baseline.

The experiment uses isolated, pinned dependencies and existing local weights.
Only model loading differs: audio/projector/head remain bf16; text linear layers
use NF4 on the same CUDA device. Production segmentation/decoding is inherited.
Saved transcripts are parsed for comparison and never executed.
"""
from __future__ import annotations

import argparse
import collections
import gc
import hashlib
import importlib.metadata
import json
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.probe_stage11_coexistence import ROOT, digest, inputs, percentiles, save

DEPENDENCIES = ROOT / ".cache/stage11-quant-deps"
SETUP = ROOT / "docs/evidence/stage11-int8-setup.json"
SKIP_MODULES = ["model.audio_tower", "model.multi_modal_projector", "lm_head"]
API_SOURCES = {
    ".venv/Lib/site-packages/transformers/utils/quantization_config.py":
        "BitsAndBytesConfig documents NF4, bf16 compute, uint8 storage and nested quantization",
    ".venv/Lib/site-packages/transformers/quantizers/quantizer_bnb_4bit.py":
        "4-bit conversion uses llm_int8_skip_modules as modules_to_not_convert",
    ".cache/stage11-quant-deps/bitsandbytes/nn/modules.py":
        "Linear4bit and Params4bit expose weight.quant_state and compute_dtype",
    ".cache/stage11-quant-deps/bitsandbytes/functional.py":
        "QuantState holds CUDA code/absmax/offset and nested state2 tensors",
}


def isolated_dependencies():
    setup = json.loads(SETUP.read_text(encoding="utf-8"))
    if not setup.get("installed") or not setup.get("project_unchanged"):
        raise RuntimeError("Verified isolated dependency setup is required")
    sys.path.insert(0, str(DEPENDENCIES))
    for name, version in setup["pins"].items():
        distribution = importlib.metadata.distribution(name)
        if (distribution.version != version
                or not Path(distribution.locate_file("")).resolve().is_relative_to(DEPENDENCIES)):
            raise RuntimeError("Experiment dependency version or location changed")
    return setup


def text_linear_names(directory):
    # Reading the safetensors header does not allocate or execute the weights.
    with (directory / "model.safetensors").open("rb") as stream:
        size = struct.unpack("<Q", stream.read(8))[0]
        header = json.loads(stream.read(size))
    return {name.removesuffix(".weight") for name, tensor in header.items()
            if name.startswith("model.language_model.layers.") and name.endswith(".weight")
            and len(tensor["shape"]) == 2}


def make_backend(catalog):
    from voicebar.stt.models import offline_environment, require_model

    offline_environment()
    directory = require_model(ROOT, "qwen", verify_hashes=True)
    vad_directory = require_model(ROOT, "silero", verify_hashes=True)
    import bitsandbytes as bnb
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig

    from voicebar.stt.base import SttError
    from voicebar.stt.context import build_context
    from voicebar.stt.parakeet import Silero
    from voicebar.stt.qwen import QwenBackend

    class ExperimentalQwen4bit(QwenBackend):
        name = "qwen-nf4-text-experiment"

        def __init__(self):
            if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
                raise SttError("CUDA with bf16 is required; CPU fallback refused")
            self.device, self.torch = "cuda", torch
            self.tensor_device, self.dtype = torch.device("cuda:0"), torch.bfloat16
            self.prompt = build_context(catalog)
            self.context_sha256 = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
            self.emit = self.model = self.processor = self.vad = None
            self.sessions, self.placement = {}, {}
            self.expected_quantized = text_linear_names(directory)
            self.quantization_config = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_storage=torch.uint8, llm_int8_enable_fp32_cpu_offload=False,
                llm_int8_skip_modules=SKIP_MODULES,
            )
            self.processor = AutoProcessor.from_pretrained(
                str(directory), local_files_only=True, trust_remote_code=False,
            )
            self.context_token_count = len(self.processor.tokenizer.encode(self.prompt, add_special_tokens=False))
            self.model = AutoModelForMultimodalLM.from_pretrained(
                str(directory), local_files_only=True, trust_remote_code=False,
                dtype=self.dtype, attn_implementation="sdpa", device_map={"": 0},
                quantization_config=self.quantization_config,
            ).eval()
            self._verify_model_placement()
            self.vad = Silero(vad_directory)

        def _verify_model_placement(self):
            super()._verify_model_placement()
            if not getattr(self.model, "is_loaded_in_4bit", False):
                raise SttError("Transformers did not confirm NF4 loading")
            # Transformers 5.19 only dispatches/attaches hf_device_map when
            # multiple devices or disk offload need hooks. Actual parameters,
            # buffers and quantizer state still must all verify on cuda:0.
            device_map = getattr(self.model, "hf_device_map", None)
            if device_map is not None and device_map != {"": 0}:
                raise SttError(f"Unexpected experiment device map: {device_map}")
            quantized = {name: module for name, module in self.model.named_modules()
                         if isinstance(module, bnb.nn.Linear4bit)}
            if not self.expected_quantized or set(quantized) != self.expected_quantized:
                raise SttError("Quantized modules differ from the existing text linear weights")
            states = []
            for name, module in quantized.items():
                if module.weight.dtype != torch.uint8 or module.weight.requires_grad:
                    raise SttError(f"Unexpected quantized weight: {name}")
                state = module.weight.quant_state
                if (state is None or state.quant_type != "nf4" or not state.nested
                        or module.compute_dtype != torch.bfloat16):
                    raise SttError(f"Unexpected NF4 compute or nested state: {name}")
                for owner_name, owner in (("state", state), ("state2", state.state2)):
                    for key, tensor in vars(owner).items():
                        if torch.is_tensor(tensor):
                            if tensor.device != self.tensor_device:
                                raise SttError(f"NF4 state left CUDA: {name}.{owner_name}.{key}")
                            states.append({"name": f"{name}.{owner_name}.{key}",
                                           "device": str(tensor.device), "dtype": str(tensor.dtype),
                                           "bytes": tensor.numel() * tensor.element_size()})
            dtype_bytes = collections.defaultdict(int)
            for parameter in self.model.parameters():
                dtype_bytes[str(parameter.dtype)] += parameter.numel() * parameter.element_size()
            self.placement.update({
                "quantized_linear_count": len(quantized), "quantized_linear_names": sorted(quantized),
                "parameter_bytes_by_dtype": dict(dtype_bytes), "hf_device_map": device_map,
                "requested_device_map": {"": 0},
                "nf4_state_tensors": states, "skipped_modules": SKIP_MODULES,
                "quantization_config": self.quantization_config.to_dict(),
                "inherited_decode": "voicebar.stt.qwen.QwenBackend._decode",
                "manifest_sha256": digest(ROOT / "models/manifest.json"),
            })

    return ExperimentalQwen4bit()


def run(baseline_path, output):
    baseline, _, loaded, items, source_hashes = inputs(baseline_path)
    setup = isolated_dependencies()
    source_hashes = {**source_hashes, "scripts/probe_stage11_nf4_speech.py": digest(Path(__file__))}
    from voicebar.stt.models import offline_environment
    offline_environment()
    import torch

    from scripts.benchmark_stage6 import _local_request
    from voicebar.parser import parse
    from voicebar.stt.base import read_wav, transcribe_segment

    if (torch.get_num_threads() != baseline["threads"]
            or torch.get_num_interop_threads() != baseline["interop_threads"]):
        raise RuntimeError("Torch host thread configuration differs from the baseline")
    if _local_request("/api/ps").get("models") != []:
        raise RuntimeError("Isolated speech comparison requires no resident Ollama models")
    report = {
        "status": "running", "verified": False, "model": "qwen-nf4-text-experiment",
        "device": "cuda", "threads": torch.get_num_threads(),
        "interop_threads": torch.get_num_interop_threads(), "session": baseline["session"],
        "scope": "unpaced final-only decode; no microphone, endpoint, gate or execution",
        "actions_executed": False, "physical_microphone_tested": False,
        "baseline": str(baseline_path), "baseline_sha256": digest(baseline_path),
        "sources": source_hashes, "settings_sha256": digest(ROOT / "config/settings.yaml"),
        "setup_sha256": digest(SETUP), "dependency_pins": setup["pins"],
        "installed_api_validation": {
            path: {"sha256": digest(ROOT / path), "reason": reason}
            for path, reason in API_SOURCES.items()
        },
        "candidate_reason": "Test weight-only NF4 kernels after int8 was slower and failed accuracy; "
                            "preserve audio/projector/head bf16 and unchanged decoding",
        "model_manifest": baseline["model_manifest"], "rows": [],
        "limitations": ["One pass; no simultaneous text or vision model",
                        "Unpaced timings are not microphone speech-end latency"],
    }
    backend = None

    def memory():
        return {"allocated": torch.cuda.memory_allocated(), "reserved": torch.cuda.memory_reserved(),
                "peak_allocated": torch.cuda.max_memory_allocated(),
                "free_and_total": list(torch.cuda.mem_get_info())}

    save(output, report)
    try:
        began = time.perf_counter()
        backend = make_backend(loaded.catalog)
        report["load_ms"] = (time.perf_counter() - began) * 1000
        report["memory_after_load"] = memory()
        if backend.context_sha256 != baseline["placement"]["context_sha256"]:
            raise RuntimeError("Vocabulary prompt differs from the unchanged baseline")
        save(output, report)
        began = time.perf_counter()
        backend.warmup()
        report["startup_warmup_ms"] = (time.perf_counter() - began) * 1000
        backend._verify_model_placement()
        report["placement_after_warm"] = backend.placement
        report["memory_after_warm"] = memory()
        torch.cuda.reset_peak_memory_stats()
        save(output, report)

        def recognize_all():
            for index, (item, previous) in enumerate(zip(items, baseline["rows"], strict=True)):
                events = []
                audio = read_wav(Path(item["wav"]))
                began = time.perf_counter()
                transcribe_segment(backend, audio, events.append)
                duration = (time.perf_counter() - began) * 1000
                text = " ".join(event.text for event in events if event.final)
                plan = parse(text, loaded.catalog)
                row = {"id": item["id"], "wav_sha256": item["wav_sha256"], "text": text,
                       "plan": plan, "expected_plan": item["expected_plan"], "stt_ms": duration,
                       "exact_plan": plan == item["expected_plan"], "baseline_plan": previous["plan"],
                       "same_plan": plan == previous["plan"], "same_transcript": text == previous["text"],
                       "generated_tokens": backend.placement.get("last_generated_tokens"),
                       "first_real_recording": index == 0}
                report["rows"].append(row)
                save(output, report)
                print(json.dumps({"model": report["model"], "done": index + 1, "total": 40,
                                  "stt_ms": round(duration, 2), "exact_plan": row["exact_plan"]}), flush=True)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="nf4-speech-probe") as pool:
            pool.submit(recognize_all).result()
        backend._verify_model_placement()
        report["placement"] = backend.placement
        report["memory_bytes"] = memory()
        rows = report["rows"]
        pairs = list(zip(baseline["rows"], rows, strict=True))
        regressions = [a["id"] for a, b in pairs if a["exact_plan"] and not b["exact_plan"]]
        wrong = [b["id"] for a, b in pairs if b["plan"] is not None
                 and not b["exact_plan"] and a["plan"] != b["plan"]]
        exact = sum(row["exact_plan"] for row in rows)
        report["summary"] = {
            "items": len(rows), "exact_plans": exact,
            "same_plan_count": sum(row["same_plan"] for row in rows),
            "same_transcript_count": sum(row["same_transcript"] for row in rows),
            "first_real_recording_ms": rows[0]["stt_ms"],
            "all": percentiles([row["stt_ms"] for row in rows]),
            "after_first": percentiles([row["stt_ms"] for row in rows[1:]]),
            "regressions": regressions, "new_wrong_actions": wrong,
            "accuracy_gate_passed": exact >= 35 and not regressions and not wrong,
        }
        report["sources_unchanged"] = source_hashes == {name: digest(ROOT / name) for name in source_hashes}
        report["settings_unchanged"] = report["settings_sha256"] == digest(ROOT / "config/settings.yaml")
        if not report["sources_unchanged"] or not report["settings_unchanged"]:
            raise RuntimeError("Sources or production settings changed during the experiment")
        report["status"] = "complete"
        report["verified"] = report["summary"]["accuracy_gate_passed"]
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if backend is not None:
            backend.close()
        gc.collect()
        torch.cuda.empty_cache()
        report["memory_after_close"] = memory()
        save(output, report)
    print(json.dumps({key: report[key] for key in
                      ("status", "verified", "load_ms", "startup_warmup_ms", "summary", "memory_bytes")},
                     indent=2), flush=True)
    return 0 if report["verified"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Preserve existing evidence; choose a new output path")
    if args.validate_only:
        baseline, _, _, items, _ = inputs(args.baseline)
        isolated_dependencies()
        print(json.dumps({"validated_recordings": len(items), "baseline_exact": baseline["summary"]["exact_plans"]}))
        return 0
    return run(args.baseline, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
