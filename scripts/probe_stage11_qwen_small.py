"""Compare pinned local Qwen sizes on the same 40 saved command recordings.

Fresh sequential processes use production decoding, prompt, parser and inputs.
No microphone, executor, desktop automation or model downloads are used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from probe_stage11_speech import SESSION, SOURCES, digest, percentiles, save

PROBE_SOURCES = (*SOURCES, "voicebar/stt/__init__.py", "voicebar/stt/models.py")


def worker(root, model, output):
    import torch

    torch.set_num_threads(16)
    if torch.get_num_threads() != 16:
        raise RuntimeError("Requested Torch host thread setting did not take effect")
    from voicebar.catalog import load_catalog
    from voicebar.parser import parse
    from voicebar.stt import load_backend
    from voicebar.stt.base import read_wav, transcribe_segment
    from voicebar.stt.benchmark import personal_items

    loaded = load_catalog(root)
    all_items, _ = personal_items(root, loaded)
    items = [row for row in all_items if row["id"].startswith(SESSION + "/")]
    if len(items) != 40:
        raise RuntimeError("The complete validated 40-recording session is required")
    source_hashes = {name: digest(root / name) for name in PROBE_SOURCES}
    manifest = json.loads((root / "models/manifest.json").read_text(encoding="utf-8"))
    result = {
        "status": "running", "model": model, "device": "cuda", "threads": 16,
        "interop_threads": torch.get_num_interop_threads(), "session": SESSION,
        "sources": source_hashes, "model_manifest": manifest["models"][model],
        "scope": "unpaced final-only decode; no microphone, endpoint, gate or execution",
        "rows": [],
    }
    save(output, result)
    began = time.perf_counter()
    backend = load_backend(model, root, loaded.catalog, device="cuda")
    result["load_ms"] = (time.perf_counter() - began) * 1000
    try:
        began = time.perf_counter()
        backend.warmup()
        result["startup_warmup_ms"] = (time.perf_counter() - began) * 1000
        torch.cuda.reset_peak_memory_stats()
        save(output, result)

        def recognize_all():
            for index, item in enumerate(items):
                audio = read_wav(Path(item["wav"]))
                events = []
                began = time.perf_counter()
                transcribe_segment(backend, audio, events.append)
                duration = (time.perf_counter() - began) * 1000
                text = " ".join(event.text for event in events if event.final)
                plan = parse(text, loaded.catalog)
                row = {
                    "id": item["id"], "wav_sha256": item["wav_sha256"],
                    "stt_ms": duration, "text": text, "plan": plan,
                    "expected_plan": item["expected_plan"], "exact_plan": plan == item["expected_plan"],
                    "first_real_recording": index == 0,
                    "generated_tokens": backend.placement.get("last_generated_tokens"),
                }
                result["rows"].append(row)
                save(output, result)
                print(json.dumps({"model": model, "done": index + 1, "total": 40,
                                  "stt_ms": round(duration, 2), "exact_plan": row["exact_plan"]}), flush=True)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-size-probe") as pool:
            pool.submit(recognize_all).result()
        result["placement"] = backend.placement
        result["memory_bytes"] = {
            "allocated": torch.cuda.memory_allocated(), "reserved": torch.cuda.memory_reserved(),
            "peak_allocated": torch.cuda.max_memory_allocated(),
            "free_and_total": list(torch.cuda.mem_get_info()),
        }
        if source_hashes != {name: digest(root / name) for name in PROBE_SOURCES}:
            raise RuntimeError("Recognition/parser source changed during the comparison")
        rows = result["rows"]
        result["summary"] = {
            "exact_plans": sum(row["exact_plan"] for row in rows), "items": len(rows),
            "first_real_recording_ms": rows[0]["stt_ms"],
            "all": percentiles([row["stt_ms"] for row in rows]),
            "after_first": percentiles([row["stt_ms"] for row in rows[1:]]),
        }
        result["status"] = "complete"
    except BaseException as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        backend.close()
        save(output, result)


def compare(baseline, candidate):
    if baseline["status"] != "complete" or candidate["status"] != "complete":
        raise RuntimeError("Both model runs must finish")
    if baseline["sources"] != candidate["sources"]:
        raise RuntimeError("Candidates used different source files")
    if baseline["placement"]["context_sha256"] != candidate["placement"]["context_sha256"]:
        raise RuntimeError("Candidates used different vocabulary prompts")
    if [(r["id"], r["wav_sha256"], r["expected_plan"]) for r in baseline["rows"]] != [
        (r["id"], r["wav_sha256"], r["expected_plan"]) for r in candidate["rows"]
    ]:
        raise RuntimeError("Candidates used different recordings or expected plans")
    pairs = list(zip(baseline["rows"], candidate["rows"], strict=True))
    regressions = [a["id"] for a, b in pairs if a["exact_plan"] and not b["exact_plan"]]
    new_wrong_actions = [b["id"] for a, b in pairs
                         if b["plan"] is not None and not b["exact_plan"] and a["plan"] != b["plan"]]
    return {
        "status": "complete", "scope": baseline["scope"],
        "baseline_model": baseline["model"], "candidate_model": candidate["model"],
        "baseline": baseline["summary"], "candidate": candidate["summary"],
        "same_plan_count": sum(a["plan"] == b["plan"] for a, b in pairs),
        "same_transcript_count": sum(a["text"] == b["text"] for a, b in pairs),
        "regressions": regressions, "new_wrong_actions": new_wrong_actions,
        "accuracy_gate_passed": candidate["summary"]["exact_plans"] >= 35 and not regressions and not new_wrong_actions,
        "improvements": [a["id"] for a, b in pairs if not a["exact_plan"] and b["exact_plan"]],
        "paired_after_first_savings_ms": percentiles([a["stt_ms"] - b["stt_ms"] for a, b in pairs[1:]]),
        "sources": baseline["sources"], "context_sha256": baseline["placement"]["context_sha256"],
        "memory_bytes": {"baseline": baseline["memory_bytes"], "candidate": candidate["memory_bytes"]},
        "limitations": ["One pass per model; qwen precedes qwen-small",
                        "No concurrent vision/text models or live microphone",
                        "Unpaced decode timings are not speech-end latency"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", choices=("qwen", "qwen-small"))
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite evidence: {output}")
    if args.worker:
        worker(root, args.worker, output)
        return
    results = []
    for model in ("qwen", "qwen-small"):
        child_output = output.with_name(f"{output.stem}-{model}.json")
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--root", str(root),
                        "--output", str(child_output), "--worker", model],
                       check=True, timeout=600, cwd=root)
        results.append(json.loads(child_output.read_text(encoding="utf-8")))
    comparison = compare(*results)
    save(output, comparison)
    print(json.dumps(comparison, indent=2), flush=True)


if __name__ == "__main__":
    main()
