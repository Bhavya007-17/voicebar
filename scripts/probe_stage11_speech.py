"""Compare Qwen host threads using saved speech, without running any actions.

Each candidate runs in a fresh, sequential process. The model, vocabulary,
recordings and expected plans are unchanged; only Torch intra-op threads vary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SESSION = "20261010T165301Z-check-0ffa4a3f"
SOURCES = (
    "voicebar/stt/qwen.py", "voicebar/stt/context.py", "voicebar/stt/base.py",
    "voicebar/stt/parakeet.py", "voicebar/parser.py", "voicebar/catalog.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentiles(values):
    ordered = sorted(values)
    return {"n": len(ordered), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1], "max_ms": max(ordered)}


def save(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def worker(root, threads, output, *, release_cache=False):
    # Set the process-wide bound before model loading or eager tensor work.
    import torch
    torch.set_num_threads(threads)
    if torch.get_num_threads() != threads:
        raise RuntimeError("Requested Torch intra-op bound did not take effect")
    from voicebar.catalog import load_catalog
    from voicebar.parser import parse
    from voicebar.stt import load_backend
    from voicebar.stt.base import read_wav, transcribe_segment
    from voicebar.stt.benchmark import personal_items

    loaded = load_catalog(root)
    all_items, _ = personal_items(root, loaded)
    items = [row for row in all_items if row["id"].startswith(SESSION + "/")]
    if len(items) != 40:
        raise RuntimeError("The complete, validated 40-recording session is required")
    source_hashes = {name: digest(root / name) for name in SOURCES}
    result = {
        "status": "running", "threads": threads, "interop_threads": torch.get_num_interop_threads(),
        "session": SESSION, "sources": source_hashes,
        "scope": "unpaced final-only decode; no microphone, endpoint, gate or execution",
        "model": "qwen", "device": "cuda", "release_cache": release_cache, "rows": [],
    }
    save(output, result)
    began = time.perf_counter()
    backend = load_backend("qwen", root, loaded.catalog, device="cuda")
    result["load_ms"] = (time.perf_counter() - began) * 1000
    try:
        began = time.perf_counter()
        backend.warmup()
        result["startup_warmup_ms"] = (time.perf_counter() - began) * 1000
        if release_cache:
            began = time.perf_counter()
            torch.cuda.empty_cache()
            result["warmup_cache_release_ms"] = (time.perf_counter() - began) * 1000
        torch.cuda.reset_peak_memory_stats()
        save(output, result)

        def recognize_all():
            for index, item in enumerate(items):
                audio = read_wav(Path(item["wav"]))
                events = []
                began = time.perf_counter()
                transcribe_segment(backend, audio, events.append)
                decoded_at = time.perf_counter()
                if release_cache:
                    torch.cuda.empty_cache()
                finished_at = time.perf_counter()
                duration = (finished_at - began) * 1000
                text = " ".join(event.text for event in events if event.final)
                plan = parse(text, loaded.catalog)
                row = {
                    "id": item["id"], "wav_sha256": item["wav_sha256"],
                    "stt_ms": duration, "text": text, "plan": plan,
                    "decode_ms": (decoded_at - began) * 1000,
                    "cache_release_ms": (finished_at - decoded_at) * 1000,
                    "expected_plan": item["expected_plan"], "exact_plan": plan == item["expected_plan"],
                    "first_real_recording": index == 0,
                    "generated_tokens": backend.placement.get("last_generated_tokens"),
                }
                result["rows"].append(row)
                save(output, result)
                print(json.dumps({"threads": threads, "done": index + 1, "total": 40,
                                  "stt_ms": round(duration, 2), "exact_plan": row["exact_plan"]}), flush=True)

        # Match the application: load/warmup and recognition use different threads.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-probe") as pool:
            pool.submit(recognize_all).result()
        result["placement"] = backend.placement
        result["memory_bytes"] = {
            "allocated": torch.cuda.memory_allocated(), "reserved": torch.cuda.memory_reserved(),
            "peak_allocated": torch.cuda.max_memory_allocated(),
            "free_and_total": list(torch.cuda.mem_get_info()),
        }
        if source_hashes != {name: digest(root / name) for name in SOURCES}:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=int, choices=(4, 16))
    parser.add_argument("--release-cache", action="store_true",
                        help="Worker-only experiment: release unused CUDA cache after warmup and each decode")
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if args.worker is not None:
        if output.exists():
            raise RuntimeError(f"Refusing to overwrite evidence: {output}")
        worker(root, args.worker, output, release_cache=args.release_cache)
        return
    if args.release_cache:
        parser.error("--release-cache requires --worker so it cannot alter the paired baseline")
    results = []
    for threads in (16, 4):
        child_output = output.with_name(f"{output.stem}-{threads}threads.json")
        if child_output.exists():
            raise RuntimeError(f"Refusing to overwrite evidence: {child_output}")
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--root", str(root),
                        "--output", str(child_output), "--worker", str(threads)],
                       check=True, timeout=600, cwd=root)
        results.append(json.loads(child_output.read_text(encoding="utf-8")))
    baseline, candidate = results
    if baseline["sources"] != candidate["sources"]:
        raise RuntimeError("Candidates used different source files")
    if baseline["placement"]["context_sha256"] != candidate["placement"]["context_sha256"]:
        raise RuntimeError("Candidates used different vocabulary prompts")
    if [(r["id"], r["wav_sha256"], r["expected_plan"]) for r in baseline["rows"]] != [
        (r["id"], r["wav_sha256"], r["expected_plan"]) for r in candidate["rows"]
    ]:
        raise RuntimeError("Candidates used different recordings or expected plans")
    pairs = list(zip(baseline["rows"], candidate["rows"], strict=True))
    comparison = {
        "status": "complete", "scope": baseline["scope"],
        "baseline": baseline["summary"], "candidate": candidate["summary"],
        "same_plan_count": sum(a["plan"] == b["plan"] for a, b in pairs),
        "same_transcript_count": sum(a["text"] == b["text"] for a, b in pairs),
        "regressions": [a["id"] for a, b in pairs if a["exact_plan"] and not b["exact_plan"]],
        "improvements": [a["id"] for a, b in pairs if not a["exact_plan"] and b["exact_plan"]],
        "paired_after_first_savings_ms": percentiles([a["stt_ms"] - b["stt_ms"] for a, b in pairs[1:]]),
        "sources": baseline["sources"], "context_sha256": baseline["placement"]["context_sha256"],
        "limitations": ["One pass per setting; 16-thread run precedes 4-thread run",
                        "No concurrent vision/text models or live microphone",
                        "Unpaced decode timings are not speech-end latency"],
    }
    save(output, comparison)
    print(json.dumps(comparison, indent=2), flush=True)


if __name__ == "__main__":
    main()
