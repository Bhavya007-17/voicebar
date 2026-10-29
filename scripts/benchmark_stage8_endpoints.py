"""Evaluate adaptive candidate boundaries on real local WAVs, without actions.

This is unpaced candidate analysis. It reports preview inference separately and
does not call a sample-clock silence reduction a live latency improvement.
Expected fixture text never enters recognition or the endpoint policy.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from voicebar.audio import FRAME_SAMPLES, SpeechSegmenter
from voicebar.catalog import _yaml_file, load_catalog
from voicebar.endpoint import PartialPolicy
from voicebar.latency import percentiles
from voicebar.parser import parse
from voicebar.stt import load_backend
from voicebar.stt.base import SAMPLE_RATE, read_wav, transcribe_segment
from voicebar.stt.models import require_model
from voicebar.stt.parakeet import Silero


def decode(backend, audio):
    events = []
    began = time.perf_counter()
    transcribe_segment(backend, audio, events.append)
    finals = {event.line_id: event for event in events if event.final}
    text = " ".join(event.text.strip() for event in sorted(
        finals.values(), key=lambda event: (event.start_sample, event.line_id)) if event.text.strip())
    return text, (time.perf_counter() - began) * 1000


def analyze(audio, segmenter, backend, policy=None):
    segments, previews = [], []
    segmenter.reset()
    for offset in range(0, len(audio), FRAME_SAMPLES):
        completed = segmenter.feed(audio[offset:offset + FRAME_SAMPLES], offset / SAMPLE_RATE)
        for preview in segmenter.drain_previews():
            text, cost = decode(backend, preview.audio)
            accepted = segmenter.update_partial(text, start=preview.start, speech_end=preview.speech_end)
            previews.append({"start": preview.start, "end": preview.end,
                             "speech_end": preview.speech_end, "text": text,
                             "stt_ms": cost, "classification": policy(text),
                             "accepted": accepted})
        for block in completed:
            text, cost = decode(backend, block.audio)
            segments.append({"start": block.start, "end": block.end,
                             "speech_end": block.speech_end, "text": text,
                             "stt_ms": cost,
                             "endpoint_ms": (block.end - block.speech_end) * 1000})
    for block in segmenter.flush():
        text, cost = decode(backend, block.audio)
        segments.append({"start": block.start, "end": block.end,
                         "speech_end": block.speech_end, "text": text,
                         "stt_ms": cost,
                         "endpoint_ms": (block.end - block.speech_end) * 1000})
    return {"segments": segments, "previews": previews,
            "text": " ".join(segment["text"] for segment in segments).strip(),
            "recognition_ms": sum(segment["stt_ms"] for segment in segments),
            "preview_ms": sum(preview["stt_ms"] for preview in previews)}


def summarize(rows, key):
    return {"recordings": len(rows), "endpoint_ms": percentiles(
        segment["endpoint_ms"] for row in rows for segment in row[key]["segments"]),
        "recognition_ms": percentiles(row[key]["recognition_ms"] for row in rows),
        "preview_ms": percentiles(row[key]["preview_ms"] for row in rows),
        "all_recognition_ms": percentiles(row[key]["recognition_ms"] + row[key]["preview_ms"]
                                          for row in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backend")
    parser.add_argument("--device")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--all-wavs", action="store_true")
    parser.add_argument("--long", type=float, default=.8)
    parser.add_argument("--short", type=float, default=.25)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "docs/evidence/stage8-endpoint-candidates.json"
    settings = _yaml_file(root / "config/settings.yaml")
    loaded = load_catalog(root)
    selected = []
    expected = {}
    for manifest in sorted((root / "fixtures/mine").glob("*/manifest.json")):
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if document.get("completed"):
            for row in document["prompts"]:
                if row.get("selected_wav"):
                    path = manifest.parent / row["selected_wav"]
                    selected.append(path)
                    expected[str(path)] = row.get("expected_plan")
    if args.all_wavs:
        selected = sorted((root / "fixtures/mine").rglob("*.wav"))
    if args.limit is not None:
        selected = selected[:args.limit]
    if not selected:
        raise SystemExit("No personal recordings found")
    backend = load_backend(args.backend or settings["stt"]["backend"], root, loaded.catalog,
                           device=args.device or settings["stt"]["device"])
    vad = Silero(require_model(root, "silero", verify_hashes=True))
    policy = PartialPolicy(loaded.catalog, names=tuple(settings.get("names", ["computer"])))
    before = SpeechSegmenter(vad, silence_seconds=.3)
    after = SpeechSegmenter(vad, silence_seconds=args.long, short_silence_seconds=args.short,
                            partial_policy=policy)
    result = {"backend": backend.name, "device": backend.device,
              "scope": "unpaced candidate analysis; preview inference is serialized and charged separately; "
                       "sample-clock endpoints are not wall-clock latency",
              "baseline_silence_seconds": .3, "long_silence_seconds": args.long,
              "short_silence_seconds": args.short, "all_wavs": args.all_wavs,
              "selected_recordings": len(selected), "rows": [], "status": "running"}
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    save()
    try:
        # The same configured backend stays resident for the entire comparison.
        # Warm with a real recording; no expected transcript or plan is provided.
        _, warmup = decode(backend, read_wav(selected[0]))
        result["warmup_ms"] = warmup
        for index, path in enumerate(selected):
            audio = read_wav(path)
            baseline = analyze(audio, before, backend)
            adaptive = analyze(audio, after, backend, policy)
            row = {"wav": str(path.relative_to(root)), "before": baseline, "after": adaptive,
                   "before_plan": parse(baseline["text"], loaded.catalog),
                   "after_plan": parse(adaptive["text"], loaded.catalog)}
            # A new boundary before later baseline speech is concrete evidence
            # of a candidate split. Whole-recording plan changes remain visible.
            row["new_early_boundaries"] = [segment["end"] for segment in adaptive["segments"]
                if any(reference["start"] <= segment["start"] < segment["end"]
                       < reference["speech_end"] - 2 / SAMPLE_RATE
                       for reference in baseline["segments"])]
            if str(path) in expected:
                row["expected_plan"] = expected[str(path)]
                row["before_exact"] = row["before_plan"] == expected[str(path)]
                row["after_exact"] = row["after_plan"] == expected[str(path)]
            result["rows"].append(row)
            save()
            print(json.dumps({"progress": f"{index + 1}/{len(selected)}", "wav": row["wav"],
                              "new_early_boundaries": row["new_early_boundaries"],
                              "before_text": baseline["text"], "after_text": adaptive["text"]}), flush=True)
        result["before"] = summarize(result["rows"], "before")
        result["after"] = summarize(result["rows"], "after")
        result["recordings_with_new_early_boundaries"] = sum(
            bool(row["new_early_boundaries"]) for row in result["rows"])
        result["status"] = "complete"
    except BaseException as error:
        result.update(status="interrupted_or_failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        backend.close()
        save()
    print(json.dumps({key: result[key] for key in ("before", "after", "recordings_with_new_early_boundaries")}))


if __name__ == "__main__":
    main()
