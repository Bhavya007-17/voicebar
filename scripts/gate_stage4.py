"""Prepare and explicitly validate local WAV -> segmentation -> STT -> gate checks.

Preparation synthesizes files only. Validation requires an explicit backend and
device; it never opens a microphone, plays audio, imports the executor, or runs a
plan. One recognition pass is replayed through continuous gates in both modes.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from voicebar.catalog import Catalog, _yaml_file, load_catalog
from voicebar.normalization import normalized_words
from voicebar.parser import parse
from voicebar.stt.corpus import (
    SYNTHETIC_RATES,
    SYNTHETIC_VOICE,
    CorpusError,
    save_manifest,
    validate_wav,
)

LONG_CONVERSATION = (
    "I thought we could take a walk after lunch if the rain finally stops this afternoon.",
    "The book I borrowed from the library has a story about a family moving to a new town.",
    "When we visited the museum last weekend, the room with the old photographs was my favorite.",
    "My neighbor told me that the flowers in the garden will probably bloom sometime next month.",
    "We should remember to bring a jacket because the weather can get chilly near the lake.",
    "I was trying to remember the name of that restaurant where we had dinner on our vacation.",
    "There is still some soup in the refrigerator if you would like something warm for lunch.",
    "The train was late this morning, so I spent a little more time reading on the platform.",
    "She said the birthday cake tasted better after it had cooled on the kitchen counter.",
    "I wonder whether the birds outside our window are building a nest in the tall tree.",
)
COMMANDS = (
    "open notepad", "open calculator", "maximize notepad",
    "minimize calculator", "scroll down", "volume up",
)
IDENTICAL_FILES = (
    ("voicebar/gate.py", "seed/voicebar/gate.py"),
    ("tests/test_gate_contract.py", "seed/tests/test_gate_contract.py"),
    ("tests/test_parser_contract.py", "seed/tests/test_parser_contract.py"),
)


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_contract_identity(root: Path) -> dict:
    result = {}
    for actual, seed in IDENTICAL_FILES:
        actual_path, seed_path = root / actual, root / seed
        if actual_path.read_bytes() != seed_path.read_bytes():
            raise CorpusError(f"{actual} is not byte-identical to {seed}.")
        result[actual] = digest(actual_path)
    return result


def conversation_from_contract(path: Path) -> list[str]:
    """Read only the literal CONVERSATION assignment; never import test code."""
    module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    matches = [node.value for node in module.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "CONVERSATION"
                       for target in node.targets)]
    if len(matches) != 1:
        raise CorpusError("Gate contract must have exactly one literal CONVERSATION assignment.")
    try:
        rows = ast.literal_eval(matches[0])
    except (ValueError, TypeError, SyntaxError) as error:
        raise CorpusError("Gate CONVERSATION must contain literal data only.") from error
    if not isinstance(rows, list) or not rows or not all(isinstance(row, str) and row for row in rows):
        raise CorpusError("Gate CONVERSATION must be a nonempty list of nonempty text.")
    return rows


def gate_configuration(root: Path, loaded) -> dict:
    from voicebar.listening import configured_gate

    gate = configured_gate(root, loaded)
    settings = _yaml_file(root / "config" / "settings.yaml")
    names = settings.get("names", [settings.get("name", "computer")])
    canonical = settings.get("name", names[0])
    if not isinstance(canonical, str) or canonical.lower() not in gate.names:
        canonical = names[0]
    return {"name": canonical, "names": names, "followup_seconds": float(gate.followup_s)}


def personal_wake_inventory(root: Path, name: str) -> dict:
    """Inspect available recording prompts, without pretending to hear their WAVs."""
    total, wake = 0, []
    for path in sorted((root / "fixtures" / "mine").glob("*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        if manifest.get("completed") is not True:
            continue
        for prompt in manifest.get("prompts", []):
            text = prompt.get("expected_text", "")
            if not isinstance(text, str):
                raise CorpusError(f"{path}: personal prompt text must be a string.")
            total += 1
            words = normalized_words(text)
            index = 0
            while index < len(words) and words[index] in {"hey", "ok", "okay"}:
                index += 1
            if index < len(words) and words[index] == normalized_words(name)[0]:
                wake.append({"manifest": str(path.relative_to(root)), "text": text})
    return {
        "completed_recording_prompts": total, "wake_name_prompts": wake,
        "verified": "prompt inventory only; personal WAVs were not transcribed by this check",
        "limitation": ("No existing personal prompt starts with the wake name; personal wake-name "
                       "recognition remains unverified." if not wake else
                       "Personal wake-name prompts exist but their audio is outside this synthetic check."),
    }


def build_manifest(root: Path, catalog: Catalog, pinned: list[str], config: dict) -> dict:
    identity = check_contract_identity(root)
    contract_rows = conversation_from_contract(root / "tests" / "test_gate_contract.py")
    for text in LONG_CONVERSATION:
        if parse(text, catalog) is not None:
            raise CorpusError(f"Long ordinary-talk fixture unexpectedly parses: {text!r}.")
    rows = [("contract_conversation", text, None) for text in contract_rows]
    rows.extend(("long_conversation", text, None) for text in LONG_CONVERSATION)
    for command in COMMANDS:
        expected = parse(command, catalog)
        if expected is None:
            raise CorpusError(f"Required synthetic command is unavailable in this catalog: {command!r}.")
        rows.extend((("named_command", f"{config['name']} {command}", expected),
                     ("unnamed_command", command, expected)))
    counters = {"command": 0, "rejected": 0}
    items = []
    # The existing native synthesis script validates its original identifiers.
    # Keep that unchanged; this manifest separately describes Stage 4 kinds.
    for kind, text, expected in rows:
        prefix = "rejected" if kind.endswith("conversation") else "command"
        counters[prefix] += 1
        for rate in SYNTHETIC_RATES:
            identifier = f"{prefix}-{counters[prefix]:03d}-rate{rate}"
            items.append({"id": identifier, "kind": kind, "text": text,
                          "expected_plan": expected, "rate": rate, "wav": f"{identifier}.wav"})
    test_apps = [parse(command, catalog)[0]["app"] for command in COMMANDS[:2]]
    return {
        "version": 1, "purpose": "Stage 4 synthetic safety diagnostics; dry-run only",
        "voice": SYNTHETIC_VOICE, "rates": list(SYNTHETIC_RATES),
        "format": {"sample_rate": 16000, "channels": 1, "sample_width": 2, "encoding": "PCM"},
        "identity": identity, "gate_config": config,
        "catalog": {"apps": list(catalog.apps), "aliases": dict(catalog.aliases),
                    "sites": dict(catalog.sites), "engines": dict(catalog.engines)},
        "pinned": list(pinned), "test_pinned": list(dict.fromkeys([*pinned, *test_apps])), "items": items,
        "test_pinning": "Configured pins plus the Notepad and Calculator test targets; configuration is unchanged.",
        "personal_wake_inventory": personal_wake_inventory(root, config["name"]),
    }


def prepare(root: Path, loaded, config: dict) -> tuple[Path, dict]:
    manifest = build_manifest(root, loaded.catalog, loaded.pinned, config)
    output = root / "fixtures" / "gate-stage4"
    output.mkdir(parents=True, exist_ok=True)
    jobs = [{"id": item["id"], "text": item["text"], "rate": item["rate"],
             "wav": str(output / item["wav"])} for item in manifest["items"]]
    request = output / "synthesis-request.json"
    save_manifest(request, {"outputRoot": str(output), "voice": manifest["voice"],
                            "sampleRate": 16000, "channels": 1, "sampleWidth": 2, "items": jobs})
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-File",
             str(root / "scripts" / "synthesize.ps1"), "-RequestPath", str(request)],
            shell=False, capture_output=True, encoding="utf-8-sig", errors="strict", check=True,
        )
        if json.loads(result.stdout) != {"count": len(jobs), "voice": manifest["voice"]}:
            raise CorpusError("Native synthesis returned an unexpected summary.")
    except subprocess.CalledProcessError as error:
        raise CorpusError(f"Native synthesis failed: {(error.stderr or error.stdout or '')[-1000:]}") from error
    for item in manifest["items"]:
        item.update(validate_wav(output / item["wav"]))
    path = output / "manifest.json"
    save_manifest(path, manifest)
    return path, manifest


def input_fingerprint(root: Path, manifest: dict, backend: str, device: str) -> str:
    sources = ("scripts/gate_stage4.py", "voicebar/audio.py", "voicebar/gate.py", "voicebar/parser.py",
               "voicebar/stt/__init__.py", "voicebar/stt/base.py", "voicebar/stt/context.py", "voicebar/stt/qwen.py",
               "voicebar/stt/parakeet.py", "voicebar/stt/models.py", "models/manifest.json")
    payload = {"manifest": manifest, "backend": backend, "device": device,
               "sources": {source: digest(root / source) for source in sources}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def corpus_segments(root: Path, manifest: dict):
    """Feed a continuous sample clock through the same segmenter used live.

    Conversation has ordinary endpoint silence. Before each command, a recorded
    silence longer than the follow-up interval deliberately isolates its wake
    requirement. No wall-clock sleep or per-utterance gate reset is involved.
    """
    import numpy as np

    from voicebar.audio import SpeechSegmenter
    from voicebar.stt.base import SAMPLE_RATE, read_wav

    segmenter = SpeechSegmenter(root=root)
    cursor = 0
    spans = []
    for item in manifest["items"]:
        if item["kind"].endswith("command"):
            gap = int((manifest["gate_config"]["followup_seconds"] + 1) * SAMPLE_RATE)
            for segment in segmenter.feed(np.zeros(gap, dtype=np.float32), cursor / SAMPLE_RATE):
                yield segment, spans
            cursor += gap
        audio = read_wav(root / "fixtures" / "gate-stage4" / item["wav"])
        spans.append({"id": item["id"], "start": cursor / SAMPLE_RATE,
                      "end": (cursor + len(audio)) / SAMPLE_RATE})
        for segment in segmenter.feed(audio, cursor / SAMPLE_RATE):
            yield segment, spans
        cursor += len(audio)
        gap = int(.65 * SAMPLE_RATE)
        for segment in segmenter.feed(np.zeros(gap, dtype=np.float32), cursor / SAMPLE_RATE):
            yield segment, spans
        cursor += gap
    for segment in segmenter.flush():
        yield segment, spans


def source_ids(segment, spans: list[dict]) -> list[str]:
    return [span["id"] for span in spans if segment.start < span["end"] and segment.end > span["start"]]


def transcribe_segment(backend, segment) -> tuple[str, float]:
    from voicebar.stt.base import transcribe_segment as decode_segment

    events = []
    began = time.perf_counter()
    decode_segment(backend, segment.audio, events.append)
    finals = {event.line_id: event for event in events if event.final}
    ordered = sorted(finals.values(), key=lambda event: (event.start_sample, event.line_id))
    return " ".join(event.text.strip() for event in ordered if event.text.strip()), (time.perf_counter() - began) * 1000


def wake_observation(text: str, item: dict, config: dict, catalog: Catalog) -> dict:
    words = normalized_words(text)
    index = 0
    while index < len(words) and words[index] in {"hey", "ok", "okay"}:
        index += 1
    token = words[index] if index < len(words) else None
    # This is evidence only. Never infer a fuzzy alias or silently change config.
    remainder = " ".join(text.split()[index + 1:])
    candidate = (token if token is not None and token not in {name.lower() for name in config["names"]}
                 and parse(remainder, catalog) == item["expected_plan"] else None)
    return {"source_id": item["id"], "intended": item["text"], "heard": text,
            "leading_token": token, "candidate_exact_variant": candidate}


def evaluate_transcripts(manifest: dict, transcripts: list[dict]) -> dict:
    from voicebar.gate import Gate

    catalog = Catalog(**manifest["catalog"])
    config = manifest["gate_config"]
    items = {item["id"]: item for item in manifest["items"]}
    wake = []
    by_mode = {}
    for mode in ("name", "open"):
        gate = Gate(catalog, names=config["names"], mode=mode,
                    followup_s=config["followup_seconds"], open_apps=manifest["test_pinned"])
        decisions = []
        plans = {identifier: [] for identifier in items}
        nonempty_transcripts = set()
        for row in transcripts:
            decision = gate.hear(row["text"], row["end"])
            result = {**row, "mode": mode, "reason": decision.reason, "plan": decision.plan}
            decisions.append(result)
            if row["text"].strip():
                nonempty_transcripts.update(row["source_ids"])
            if decision.reason == "run":
                for identifier in row["source_ids"]:
                    plans[identifier].extend(decision.plan or [])
            if mode == "name":
                for identifier in row["source_ids"]:
                    item = items[identifier]
                    if item["kind"] == "named_command":
                        wake.append(wake_observation(row["text"], item, config, catalog))
        failures = []
        conversation_plans = []
        for identifier, item in items.items():
            actual = plans[identifier]
            if identifier not in nonempty_transcripts:
                failures.append({"source_id": identifier, "check": "source_transcription_missing", "actual": actual})
            if item["kind"].endswith("conversation") and actual:
                conversation_plans.append({"source_id": identifier, "source_text": item["text"], "plans": actual})
                failures.append({"source_id": identifier, "check": "conversation_zero_plans", "actual": actual})
            elif item["kind"] == "named_command" and actual != item["expected_plan"]:
                failures.append({"source_id": identifier, "check": "named_command_runs",
                                 "expected": item["expected_plan"], "actual": actual})
            elif mode == "name" and item["kind"] == "unnamed_command" and actual:
                failures.append({"source_id": identifier, "check": "unnamed_name_mode_ignored", "actual": actual})
        by_mode[mode] = {"passed": not failures, "failures": failures,
                         "conversation_plans": conversation_plans, "decisions": decisions}
    return {"passed": all(value["passed"] for value in by_mode.values()), "modes": by_mode,
            "wake_observations": wake,
            "candidate_exact_variants": sorted({row["candidate_exact_variant"] for row in wake
                                                if row["candidate_exact_variant"]}),
            "personal_wake_inventory": manifest["personal_wake_inventory"]}


def validate(root: Path, loaded, config: dict, *, backend_name: str, device: str,
             resume: Path | None = None) -> tuple[Path, dict]:
    from voicebar.stt import load_backend
    from voicebar.stt.offline import SocketAuditPolicy

    path = root / "fixtures" / "gate-stage4" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = build_manifest(root, loaded.catalog, loaded.pinned, config)
    for field in expected:
        if field != "items" and manifest.get(field) != expected[field]:
            raise CorpusError(f"Synthetic manifest {field} changed; prepare the corpus again.")
    if len(manifest["items"]) != len(expected["items"]):
        raise CorpusError("Synthetic manifest item count changed; prepare again.")
    for item, reference in zip(manifest["items"], expected["items"], strict=True):
        if any(item.get(key) != value for key, value in reference.items()):
            raise CorpusError(f"Synthetic case provenance changed: {reference['id']}.")
        actual = validate_wav(path.parent / item["wav"])
        if any(item.get(key) != value for key, value in actual.items()):
            raise CorpusError(f"Synthetic WAV changed: {item['id']}.")
    fingerprint = input_fingerprint(root, manifest, backend_name, device)
    if resume is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run = root / "docs" / "evidence" / f"stage4-wav-{stamp}-{uuid.uuid4().hex[:8]}"
        run.mkdir(parents=True)
        save_manifest(run / "run.json", {"fingerprint": fingerprint, "backend": backend_name,
                                         "device": device, "complete": False})
    else:
        run = Path(resume).resolve()
        if not run.is_relative_to((root / "docs" / "evidence").resolve()):
            raise CorpusError("Resume directory must be inside docs/evidence/.")
        previous = json.loads((run / "run.json").read_text(encoding="utf-8"))
        if previous.get("fingerprint") != fingerprint or previous.get("complete"):
            raise CorpusError("Resume requires an incomplete run with identical code, config, corpus and backend.")
    checkpoint = run / "transcripts.jsonl"
    cached = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()] if checkpoint.exists() else []
    transcripts = []
    policy = SocketAuditPolicy()
    policy.install_and_check()
    backend = None
    try:
        backend = load_backend(backend_name, root, loaded.catalog, device=device)
        with checkpoint.open("a", encoding="utf-8") as stream:
            for index, (segment, spans) in enumerate(corpus_segments(root, manifest)):
                identity = {"segment_id": index, "start": segment.start, "end": segment.end,
                            "speech_end": segment.speech_end, "source_ids": source_ids(segment, spans)}
                if not identity["source_ids"]:
                    raise CorpusError("Speech segment does not intersect any synthesized utterance.")
                if index < len(cached):
                    row = cached[index]
                    if any(row.get(key) != value for key, value in identity.items()):
                        raise CorpusError("Segment boundaries changed; refuse checkpoint replay.")
                else:
                    text, decode_ms = transcribe_segment(backend, segment)
                    row = {**identity, "text": text, "decode_ms": decode_ms}
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                transcripts.append(row)
                print(json.dumps({"segment": index, "source_ids": row["source_ids"], "text": row["text"],
                                  "cached": index < len(cached)}), flush=True)
        if len(transcripts) < len(cached):
            raise CorpusError("Checkpoint has more speech segments than the current corpus.")
        report = evaluate_transcripts(manifest, transcripts)
        runtime_events = [event for event in policy.events if event["phase"] == "runtime"]
        if runtime_events:
            raise CorpusError("Runtime attempted network use; synthetic validation fails.")
        report.update({"backend": backend_name, "device": device, "fingerprint": fingerprint,
                       "wav_count": len(manifest["items"]), "segment_count": len(transcripts),
                       "identity": check_contract_identity(root), "configured_pins": manifest["pinned"],
                       "effective_test_pins": manifest["test_pinned"],
                       "runtime_details": dict(getattr(backend, "runtime_details", getattr(backend, "placement", {}))),
                       "network": {"python_socket_attempts": runtime_events,
                                   "limitation": "Python audit refusal only; native sockets were not monitored."},
                       "limitations": ["Synthetic speech does not verify personal wake-name audio.",
                                       "Unpaced replay does not establish live end-to-action latency.",
                                       "No microphone, session lock or real executor action was tested.",
                                       "Dry-run execution has zero simulated duration."]})
        save_manifest(run / "report.json", report)
        save_manifest(run / "run.json", {"fingerprint": fingerprint, "backend": backend_name,
                                         "device": device, "complete": True, "passed": report["passed"]})
        return run / "report.json", report
    except BaseException as error:
        save_manifest(run / f"failure-{uuid.uuid4().hex[:8]}.json", {
            "type": type(error).__name__, "error": str(error), "fingerprint": fingerprint,
            "completed_segments": len(transcripts), "resume_directory": str(run),
            "python_socket_events": policy.events,
        })
        raise
    finally:
        if backend is not None:
            backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-only", action="store_true")
    action.add_argument("--validate", action="store_true")
    parser.add_argument("--backend")
    parser.add_argument("--device", choices=("cpu", "cuda"))
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    if args.validate and (args.backend is None or args.device is None):
        parser.error("--validate requires explicit --backend and --device; no configured fallback is used")
    if args.prepare_only and (args.backend is not None or args.device is not None or args.resume is not None):
        parser.error("--prepare-only does not accept inference options")
    root = args.project_root.resolve()
    try:
        loaded = load_catalog(root)
        config = gate_configuration(root, loaded)
        if args.prepare_only:
            path, manifest = prepare(root, loaded, config)
            print(json.dumps({"manifest": str(path), "wav_count": len(manifest["items"]),
                              "prepared_only": True, "personal_wake_inventory": manifest["personal_wake_inventory"]}, indent=2))
            return 0
        path, report = validate(root, loaded, config, backend_name=args.backend, device=args.device, resume=args.resume)
        print(json.dumps({"report": str(path), "passed": report["passed"],
                          "modes": {key: {"passed": value["passed"], "failures": value["failures"],
                                          "conversation_plans": value["conversation_plans"]}
                                    for key, value in report["modes"].items()},
                          "candidate_exact_variants": report["candidate_exact_variants"]}, indent=2))
        return 0 if report["passed"] else 1
    except (CorpusError, ValueError, OSError, RuntimeError) as error:
        parser.exit(1, f"Stage 4 WAV check failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
