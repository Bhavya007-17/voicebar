"""Verify saved speech and an owned Calculator with all local models resident.

Saved recognition transcripts are parsed and compared, never executed. Only the
existing desktop probe may execute its fixed, guarded owned-Calculator cases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = "20261010T165301Z-check-0ffa4a3f"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def percentiles(values):
    ordered = sorted(values)
    return {"n": len(ordered), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1], "max_ms": max(ordered)}


def inputs(baseline_path):
    from voicebar.catalog import _yaml_file, load_catalog
    from voicebar.stt.benchmark import personal_items
    from voicebar.stt.context import build_context

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    settings = _yaml_file(ROOT / "config/settings.yaml")
    stt = settings["stt"]
    if (baseline.get("status") != "complete" or baseline.get("session") != SESSION
            or baseline.get("model") != "qwen" or baseline.get("device") != "cuda"
            or stt != {"backend": "qwen", "device": "cuda"}
            or baseline.get("summary", {}).get("exact_plans") != 35):
        raise RuntimeError("A complete configured-Qwen 35/40 baseline is required")
    source_hashes = {name: digest(ROOT / name) for name in baseline["sources"]}
    if source_hashes != baseline["sources"]:
        raise RuntimeError("Recognition/parser source differs from the fresh baseline")
    manifest = json.loads((ROOT / "models/manifest.json").read_text(encoding="utf-8"))
    if manifest["models"]["qwen"] != baseline["model_manifest"]:
        raise RuntimeError("Configured Qwen model manifest differs from the fresh baseline")
    loaded = load_catalog(ROOT)
    all_items, _ = personal_items(ROOT, loaded)
    items = [row for row in all_items if row["id"].startswith(SESSION + "/")]
    if len(items) != 40 or len(baseline["rows"]) != 40:
        raise RuntimeError("All 40 validated recordings are required")
    for item, previous in zip(items, baseline["rows"], strict=True):
        if any(item[key] != previous[key] for key in ("id", "wav_sha256", "expected_plan")):
            raise RuntimeError("Recording identity or unchanged expected plan differs from the baseline")
    prompt_hash = hashlib.sha256(build_context(loaded.catalog).encode("utf-8")).hexdigest()
    if prompt_hash != baseline["placement"]["context_sha256"]:
        raise RuntimeError("Vocabulary prompt differs from the fresh baseline")
    if (settings["resolver"]["backend"] != "ollama"
            or settings["resolver"]["model"] != "qwen2.5:3b"
            or not settings["vision"]["enabled"]
            or settings["vision"]["model"] != "qwen3-vl:2b-instruct"
            or settings["resolver"]["port"] != 11434
            or settings["vision"]["timeout_ms"] != 5000):
        # The existing desktop probe uses these production defaults. Refuse a
        # mismatch instead of silently probing another model or deadline.
        raise RuntimeError("Configured selectors differ from the existing desktop probe")
    return baseline, settings, loaded, items, source_hashes


def run(baseline_path, output, desktop_output):
    baseline, settings, loaded, items, source_hashes = inputs(baseline_path)
    import torch

    from voicebar.parser import parse
    from voicebar.resolver.ollama import OllamaBackend
    from voicebar.resolver.vision import VisionSelector
    from voicebar.stt import load_backend
    from voicebar.stt.base import read_wav, transcribe_segment

    if (torch.get_num_threads() != baseline["threads"]
            or torch.get_num_interop_threads() != baseline["interop_threads"]):
        raise RuntimeError("Torch thread configuration differs from the fresh baseline")
    text_selector = OllamaBackend(settings["resolver"]["model"], port=settings["resolver"]["port"])
    vision_selector = VisionSelector(settings["vision"]["model"], port=settings["resolver"]["port"])
    required = {text_selector.model, vision_selector.backend.model}
    report = {
        "time": datetime.now(UTC).isoformat(), "status": "running", "verified": False,
        "physical_microphone_tested": False, "saved_transcripts_executed": False,
        "screenshots_persisted": False, "baseline": str(baseline_path),
        "baseline_sha256": digest(baseline_path), "sources": source_hashes,
        "settings_sha256": digest(ROOT / "config/settings.yaml"),
        "session": SESSION, "model": settings["stt"]["backend"], "device": settings["stt"]["device"],
        "selector_options": {"text": dict(text_selector._OPTIONS), "vision": dict(vision_selector._OPTIONS)},
        "scope": "final-only saved speech followed by guarded owned-Calculator replay; no microphone",
        "rows": [], "snapshots": {}, "desktop_output": str(desktop_output),
    }
    backend = None

    def residency():
        reply = text_selector._request("/api/ps", timeout_s=2)
        models = reply.get("models")
        if not isinstance(models, list):
            raise TypeError("Local model residency metadata is unavailable")
        return [{key: row.get(key) for key in ("name", "expires_at", "size_vram", "size", "context_length")}
                for row in models]

    def snapshot(label, *, require_resident=False):
        models = residency()
        value = {"models": models, "asr_live": backend is not None and backend.model is not None,
                 "cuda_bytes": {"allocated": torch.cuda.memory_allocated(),
                                "reserved": torch.cuda.memory_reserved(),
                                "peak_allocated": torch.cuda.max_memory_allocated(),
                                "free_and_total": list(torch.cuda.mem_get_info())}}
        report["snapshots"][label] = value
        save(output, report)
        if require_resident and not required <= {row["name"] for row in models}:
            raise RuntimeError(f"Required text/vision model is not resident at {label}")
        return value

    def check_sources():
        if source_hashes != {name: digest(ROOT / name) for name in source_hashes}:
            raise RuntimeError("Recognition/parser source changed during the coexistence check")
        if report["settings_sha256"] != digest(ROOT / "config/settings.yaml"):
            raise RuntimeError("Runtime settings changed during the coexistence check")

    save(output, report)
    try:
        started = time.perf_counter()
        backend = load_backend(settings["stt"]["backend"], ROOT, loaded.catalog, device=settings["stt"]["device"])
        report["load_ms"] = (time.perf_counter() - started) * 1000
        if backend.placement["context_sha256"] != baseline["placement"]["context_sha256"]:
            raise RuntimeError("Loaded model received a different vocabulary prompt")
        started = time.perf_counter()
        backend.warmup()
        report["speech_warmup_ms"] = (time.perf_counter() - started) * 1000
        snapshot("after_speech_warmup")
        # The controller starts vision preload before constructing the text
        # resolver. Use those exact clients/options, with no competing warmup.
        for name, selector in (("vision", vision_selector), ("text", text_selector)):
            started = time.perf_counter()
            ok = selector.warm(timeout_s=60)
            report[name + "_warmup"] = {"ok": ok, "ms": (time.perf_counter() - started) * 1000}
            save(output, report)
            if not ok:
                raise RuntimeError(f"Production {name} selector warmup failed")
        snapshot("before_recordings", require_resident=True)
        torch.cuda.reset_peak_memory_stats()

        def recognize_all():
            for index, (item, previous) in enumerate(zip(items, baseline["rows"], strict=True)):
                audio = read_wav(Path(item["wav"]))
                events = []
                started = time.perf_counter()
                transcribe_segment(backend, audio, events.append)
                duration = (time.perf_counter() - started) * 1000
                text = " ".join(event.text for event in events if event.final)
                plan = parse(text, loaded.catalog)
                # Metadata calls happen after timing. They neither execute
                # transcripts nor generate text/images nor reload a model.
                resident = residency()
                row = {"id": item["id"], "wav_sha256": item["wav_sha256"], "stt_ms": duration,
                       "text": text, "plan": plan, "expected_plan": item["expected_plan"],
                       "exact_plan": plan == item["expected_plan"], "baseline_plan": previous["plan"],
                       "same_plan": plan == previous["plan"], "same_transcript": text == previous["text"],
                       "first_real_recording": index == 0, "resident_models": [r["name"] for r in resident]}
                report["rows"].append(row)
                save(output, report)
                if not required <= set(row["resident_models"]):
                    raise RuntimeError("A required selector is no longer resident during saved-speech replay")
                print(json.dumps({"event": "speech", "done": index + 1, "total": 40,
                                  "stt_ms": round(duration, 2), "exact_plan": row["exact_plan"]}), flush=True)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="coexistence-speech") as pool:
            pool.submit(recognize_all).result()
        rows = report["rows"]
        report["placement"] = backend.placement
        report["speech_summary"] = {
            "items": len(rows), "exact_plans": sum(row["exact_plan"] for row in rows),
            "same_plan_count": sum(row["same_plan"] for row in rows),
            "same_transcript_count": sum(row["same_transcript"] for row in rows),
            "plan_changes": [row["id"] for row in rows if not row["same_plan"]],
            "first_real_recording_ms": rows[0]["stt_ms"],
            "all": percentiles([row["stt_ms"] for row in rows]),
            "after_first": percentiles([row["stt_ms"] for row in rows[1:]]),
        }
        snapshot("after_recordings", require_resident=True)
        check_sources()
        if report["speech_summary"]["exact_plans"] != 35 or report["speech_summary"]["same_plan_count"] != 40:
            raise RuntimeError("Coexistence changed the accepted 35/40 command result or a baseline plan")
        # This remains the main thread so the existing Qt probe owns its event
        # loop. Keep the speech backend alive until its final cleanup below.
        import probe_stage11_desktop
        previous_argv = sys.argv
        sys.argv = [str(ROOT / "scripts/probe_stage11_desktop.py"), "--output", str(desktop_output)]
        try:
            report["desktop_exit_code"] = probe_stage11_desktop.main()
        finally:
            sys.argv = previous_argv
        if not desktop_output.is_file():
            raise RuntimeError("Owned desktop probe did not produce its evidence")
        desktop = json.loads(desktop_output.read_text(encoding="utf-8"))
        report["desktop"] = {key: desktop.get(key) for key in
                             ("verified", "owned_window_closed", "physical_microphone_tested", "screenshots_persisted")}
        report["desktop_sha256"] = digest(desktop_output)
        snapshot("after_desktop", require_resident=True)
        check_sources()
        if (report["desktop_exit_code"] != 0 or desktop.get("verified") is not True
                or desktop.get("owned_window_closed") is not True or backend.model is None):
            raise RuntimeError("Owned desktop probe or concurrent speech residency did not verify")
        report["status"], report["verified"] = "complete", True
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - preserve failed native/inference evidence
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if backend is not None:
            try:
                backend.close()
                torch.cuda.empty_cache()
                report["speech_closed"] = backend.model is None
            except Exception as error:  # noqa: BLE001 - cleanup failure must invalidate the result
                report["status"], report["verified"] = "failed", False
                report["cleanup_error"] = f"{type(error).__name__}: {error}"
        save(output, report)
    print(json.dumps({key: report.get(key) for key in
                      ("status", "verified", "error", "speech_summary", "desktop", "speech_closed")}), flush=True)
    return 0 if report["verified"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "docs/evidence/stage11-qwen-size-qwen.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/stage11-coexistence.json")
    parser.add_argument("--validate-only", action="store_true", help="Validate files/hashes without loading models or using UI")
    args = parser.parse_args()
    baseline_path, output = args.baseline.resolve(), args.output.resolve()
    if args.validate_only:
        baseline, _, _, items, _ = inputs(baseline_path)
        print(json.dumps({"validated": True, "items": len(items), "baseline_exact_plans": baseline["summary"]["exact_plans"],
                          "models_loaded": False, "desktop_actions": False}), flush=True)
        return 0
    desktop_output = output.with_name(output.stem + "-desktop.json")
    if output.exists() or desktop_output.exists():
        raise RuntimeError("Refusing to overwrite existing coexistence evidence")
    return run(baseline_path, output, desktop_output)


if __name__ == "__main__":
    raise SystemExit(main())
