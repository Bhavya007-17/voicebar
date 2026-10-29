"""Local STT evaluation with exact plans and an explicit prerecorded audio clock.

Silero's last active 512-sample frame estimates speech end; it is not a manual
phonetic annotation. Unpaced decode time is never reported as EOS latency.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np

from ..catalog import LoadedCatalog, load_catalog
from ..parser import parse
from ..recording import build_prompts
from ..schema import validate_plan
from . import load_backend, model_assets
from .base import SAMPLE_RATE, read_wav, transcribe_wav
from .corpus import validate_wav
from .models import require_model, sha256

FRAME_SAMPLES = 512
ONSET = 0.5
CONTINUATION = 0.35
REFERENCE_SILENCE_SAMPLES = int(0.3 * SAMPLE_RATE)


class BenchmarkError(ValueError):
    """Invalid benchmark data or incompatible resume checkpoint."""


def runtime_packages() -> dict:
    result = {}
    for name in ("moonshine-voice", "onnx-asr", "onnxruntime", "onnxruntime-gpu", "numpy",
                 "torch", "transformers", "huggingface-hub", "tokenizers", "faster-whisper", "ctranslate2"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise TypeError("document must be an object")
        return value
    except (OSError, ValueError, TypeError) as error:
        raise BenchmarkError(f"Cannot read {path}: {error}") from error


def _plan(value, label: str, *, nullable=True):
    if value is None and nullable:
        return None
    try:
        return validate_plan(value)
    except ValueError as error:
        raise BenchmarkError(f"{label}: {error}") from error


def personal_items(root: Path, loaded: LoadedCatalog) -> tuple[list[dict], list[dict]]:
    """Select only accepted chosen takes from complete 40-prompt sessions.

    Expected text/plans and contract/catalog/prompt provenance must still match
    the saved recording contract. Rejected/retried takes never enter the set.
    """
    root = Path(root).resolve()
    prompts, provenance = build_prompts(root, loaded)
    base = (root / "fixtures" / "mine").resolve()
    items, excluded = [], []
    for path in sorted(base.glob("*/manifest.json")):
        manifest = _read_json(path)
        if manifest.get("completed") is not True:
            excluded.append({"manifest": str(path), "reason": "session is incomplete"})
            continue
        if (manifest.get("version") != 1 or manifest.get("provenance") != provenance
                or manifest.get("audio_format") != {"sample_rate": SAMPLE_RATE, "channels": 1, "sample_width": 2}
                or not isinstance(manifest.get("prompts"), list) or len(manifest["prompts"]) != 40):
            raise BenchmarkError(f"Completed session has changed provenance/format/count: {path}")
        session = path.parent.resolve()
        if session.parent != base:
            raise BenchmarkError("Recording session leaves fixtures/mine.")
        label = manifest.get("session_label")
        if not isinstance(label, str) or not label:
            raise BenchmarkError(f"Completed session has no condition label: {path}")
        for expected, entry in zip(prompts, manifest["prompts"], strict=True):
            if any(entry.get(key) != value for key, value in expected.items()) or entry.get("status") != "recorded":
                raise BenchmarkError(f"Completed session prompt changed or is unrecorded: {session.name}/{expected['id']}")
            filename = entry.get("selected_wav")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.startswith(expected["id"] + "-take-") or not filename.endswith(".wav"):
                raise BenchmarkError(f"Invalid selected take path: {session.name}/{expected['id']}")
            selected = [take for take in entry.get("attempts", []) if take.get("wav") == filename
                        and take.get("decision") == "keep"]
            if len(selected) != 1 or selected[0].get("quality", {}).get("accepted") is not True:
                raise BenchmarkError(f"Selected take is not uniquely kept and accepted: {filename}")
            wav = (session / filename).resolve()
            if wav.parent != session:
                raise BenchmarkError("Selected take leaves its session directory.")
            info = validate_wav(wav)
            if (info["wav_sha256"] != selected[0].get("wav_sha256")
                    or info["frames"] != selected[0]["quality"].get("frames")):
                raise BenchmarkError(f"Selected take checksum/frame count changed: {filename}")
            _plan(entry["expected_plan"], filename, nullable=False)
            items.append({
                "id": f"{session.name}/{entry['id']}", "text": entry["expected_text"],
                "wav": str(wav), "wav_sha256": info["wav_sha256"],
                "expected_plan": entry["expected_plan"], "last_app": None,
                "condition": label, "source": {
                    "manifest": str(path), "manifest_sha256": sha256(path),
                    "prompt_id": entry["id"], "source_index": entry["source_index"],
                    "source_text": entry["source_text"], "provenance": provenance,
                },
            })
    if not items:
        raise BenchmarkError("No completed 40-prompt personal recording sessions were found.")
    return items, excluded


def synthetic_items(root: Path, loaded: LoadedCatalog) -> tuple[list[dict], list[dict]]:
    root = Path(root).resolve()
    path = root / "fixtures" / "synthetic" / "manifest.json"
    manifest = _read_json(path)
    contract_hash = sha256(root / "tests" / "test_parser_contract.py")
    snapshot = {"apps": loaded.catalog.apps, "aliases": loaded.catalog.aliases,
                "sites": loaded.catalog.sites, "engines": loaded.catalog.engines}
    if manifest.get("contract_sha256") != contract_hash or manifest.get("catalog") != snapshot:
        raise BenchmarkError("Synthetic contract/catalog provenance changed; regenerate the corpus.")
    if not isinstance(manifest.get("items"), list) or len(manifest["items"]) != 288:
        raise BenchmarkError("Synthetic manifest must contain all 288 contract/rate evaluations.")
    items, identifiers = [], set()
    for entry in manifest["items"]:
        identifier, filename = entry.get("id"), entry.get("wav")
        if (not isinstance(identifier, str) or identifier in identifiers or not isinstance(filename, str)
                or Path(filename).name != filename or filename != f"{identifier}.wav"
                or entry.get("contract_sha256") != contract_hash or "expected_plan" not in entry):
            raise BenchmarkError("Synthetic item identity/provenance is invalid.")
        identifiers.add(identifier)
        wav = (path.parent / filename).resolve()
        if wav.parent != path.parent.resolve():
            raise BenchmarkError("Synthetic WAV path leaves its corpus directory.")
        info = validate_wav(wav)
        if info["wav_sha256"] != entry.get("wav_sha256") or info["frames"] != entry.get("frames"):
            raise BenchmarkError(f"Synthetic WAV checksum/frame count changed: {filename}")
        _plan(entry["expected_plan"], filename)
        _plan(entry.get("reference_plan"), filename)
        items.append({**entry, "wav": str(wav), "condition": f"rate{entry['rate']}",
                      "source": {**entry["source"], "manifest": str(path), "manifest_sha256": sha256(path)}})
    return items, []


def _select_sessions(root: Path, items: list[dict], excluded: list[dict], sessions):
    """Filter already validated personal data by exact session directory names.

    Selection never bypasses provenance checks on the complete corpus. Every
    requested session must contribute all 40 validated prompts; misspellings,
    incomplete sessions and directories without manifests cannot disappear.
    """
    if sessions is None:
        return items, excluded, None
    if not isinstance(sessions, (list, tuple)) or not sessions:
        raise BenchmarkError("sessions must be a nonempty list of exact session directory names.")
    for name in sessions:
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or Path(name).name != name):
            raise BenchmarkError("Each session must be an exact directory name under fixtures/mine.")
    selected = sorted(set(sessions))
    base = root / "fixtures" / "mine"
    existing = {path.name for path in base.iterdir() if path.is_dir()}
    unknown = [name for name in selected if name not in existing]
    if unknown:
        raise BenchmarkError(f"Unknown session directory name(s): {', '.join(unknown)}")
    grouped = {}
    for item in items:
        manifest = item["source"]["manifest"]
        name = Path(manifest).parent.name
        grouped.setdefault(name, []).append(item)
    for name in selected:
        if len(grouped.get(name, [])) != 40:
            raise BenchmarkError(f"Requested session is not a complete validated 40-prompt session: {name}")
    filtered = [item for item in items if Path(item["source"]["manifest"]).parent.name in selected]
    exclusions = [*excluded, *[
        {"manifest": rows[0]["source"]["manifest"], "reason": "session was not selected"}
        for name, rows in sorted(grouped.items()) if name not in selected
    ]]
    return filtered, exclusions, selected


def speech_end(audio, vad) -> dict:
    """Estimate final speech sample with a common reset Silero CPU reference.

    Detect onset at 0.5 and continuation at 0.35. The estimate is the end of the
    last active 512-sample frame, clamped to file length; uncertainty is at least
    one frame (32 ms), plus model classification error. No extra endpoint wait
    is added to the reference itself.
    """
    vad.reset()
    active, end, quiet_samples = False, None, 0
    for offset in range(0, len(audio), FRAME_SAMPLES):
        frame = audio[offset:offset + FRAME_SAMPLES]
        if len(frame) < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - len(frame)))
        probability = vad.probability(frame)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise BenchmarkError("Common Silero reference returned an invalid probability.")
        if probability >= ONSET:
            active = True
        if active and probability >= CONTINUATION:
            end = min(offset + FRAME_SAMPLES, len(audio))
            quiet_samples = 0
        elif active:
            quiet_samples += FRAME_SAMPLES
            if quiet_samples >= REFERENCE_SILENCE_SAMPLES:
                active = False
                quiet_samples = 0
    return {"speech_end_sample": end, "method": "silero-last-active-frame",
            "sample_rate": SAMPLE_RATE, "frame_samples": FRAME_SAMPLES,
            "frame_resolution_ms": FRAME_SAMPLES / SAMPLE_RATE * 1000,
            "onset_threshold": ONSET, "continuation_threshold": CONTINUATION,
            "reset_after_silence_samples": REFERENCE_SILENCE_SAMPLES,
            "limitation": "estimated speech end; model errors and 32 ms frame quantization are possible"}


def replay_paced(backend, audio, end_sample: int | None, *, clock=time.perf_counter, sleep=time.sleep) -> dict:
    """Pace samples on an audio clock, preserving backlog and early final times."""
    events = []
    began = clock()
    backend.start(events.append)
    try:
        for offset in range(0, len(audio), FRAME_SAMPLES):
            chunk = audio[offset:offset + FRAME_SAMPLES]
            deadline = began + (offset + len(chunk)) / SAMPLE_RATE
            remaining = deadline - clock()
            if remaining > 0:
                sleep(remaining)  # Recorded sample arrival clock, not a readiness workaround.
            backend.feed(chunk)
        backend.finish()
    except BaseException:
        backend.close()
        raise
    ended = clock()
    finals = {}
    for event in events:
        if event.final:
            finals[event.line_id] = event
    ordered = sorted(finals.values(), key=lambda event: (event.start_sample, event.line_id))
    final_at = max((event.emitted_at for event in ordered), default=None)
    release_at = began + len(audio) / SAMPLE_RATE
    return {
        "backend": backend.name, "device": backend.device,
        "text": " ".join(event.text.strip() for event in ordered if event.text.strip()),
        "timing_mode": "realtime-replay", "audio_seconds": len(audio) / SAMPLE_RATE,
        "decode_ms": None, "replay_elapsed_ms": (ended - began) * 1000,
        "release_to_final_ms": (final_at - release_at) * 1000 if final_at is not None else None,
        "last_speech_to_final_ms": (final_at - (began + end_sample / SAMPLE_RATE)) * 1000
        if final_at is not None and end_sample is not None else None,
        "final_event_count": len(ordered),
        "hotwords_supported": backend.hotwords_supported, "partials_kind": backend.partials_kind,
        "events": [asdict(event) for event in events],
    }


def _app_targets(plan) -> list[str]:
    if plan is None:
        return []
    return [action["app"] if "app" in action else action["target"]
            for action in plan if "app" in action or (action.get("action") == "window" and action.get("target") != "active")]


def score(item: dict, result: dict, catalog) -> dict:
    """Full dictionary/list equality is the metric; no expected-plan rewriting."""
    plan = parse(result["text"], catalog, last_app=item.get("last_app"))
    if plan is not None:
        validate_plan(plan)
    expected = item["expected_plan"]
    expected_apps, actual_apps = _app_targets(expected), _app_targets(plan)
    app_error = None
    if expected_apps and expected_apps != actual_apps:
        app_error = {"kind": "wrong_app_target" if actual_apps else "unresolved_with_app_target",
                     "expected": expected_apps, "actual": actual_apps}
    return {**result, "id": item["id"], "wav": item["wav"], "wav_sha256": item["wav_sha256"],
            "expected_text": item["text"], "expected_plan": expected, "plan": plan,
            "exact_plan": plan == expected, "null_plan": plan is None,
            "app_error": app_error, "condition": item["condition"],
            "reference_plan": item.get("reference_plan"),
            "reference_exact_plan": plan == item["reference_plan"] if "reference_plan" in item else None,
            "source": item["source"]}


def percentiles(values) -> dict:
    ordered = sorted(value for value in values if value is not None and math.isfinite(value))
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    return {"count": len(ordered), "p50": ordered[math.ceil(len(ordered) * 0.5) - 1],
            "p95": ordered[math.ceil(len(ordered) * 0.95) - 1]}


def summarize(rows: list[dict]) -> dict:
    valid = [row for row in rows if "error" not in row]
    exact = sum(row.get("exact_plan") is True for row in valid)
    eos = [row.get("last_speech_to_final_ms") for row in valid if row.get("timing_mode") == "realtime-replay"]
    return {
        "items": len(rows), "successful_results": len(valid), "errors": len(rows) - len(valid),
        "exact_plans": exact, "exact_plan_accuracy": exact / len(rows) if rows else None,
        "null_plans": sum(row.get("null_plan") is True for row in valid),
        "app_errors": dict(Counter(row["app_error"]["kind"] for row in valid if row.get("app_error"))),
        "last_speech_to_final_ms": percentiles(eos),
        "release_to_final_ms": percentiles(row.get("release_to_final_ms") for row in valid
                                             if row.get("timing_mode") == "realtime-replay"),
        "unpaced_decode_ms": percentiles(row.get("decode_ms") for row in valid
                                         if row.get("timing_mode") == "unpaced-batch"),
        "early_finals_before_estimated_speech_end": sum(value is not None and value < 0 for value in eos),
        "missing_speech_end_or_final": sum(value is None for value in eos),
        "timing_modes": dict(Counter(row.get("timing_mode", "error") for row in rows)),
    }


def reuse_offline_proof(path: Path, items: list[dict], backend: str, device: str, catalog) -> dict:
    """Reuse complete proof transcripts for accuracy only, never for EOS timing."""
    proof = _read_json(Path(path))
    summaries = [row for row in proof.get("records", []) if row.get("kind") == "summary"]
    results = [row for row in proof.get("records", []) if row.get("kind") == "result"]
    if (proof.get("ok") is not True or proof.get("backend") != backend or proof.get("device") != device
            or proof.get("returncode") != 0 or proof.get("ready_before_load") is not True
            or proof.get("violations") or proof.get("audit_runtime_attempts") or proof.get("monitoring_error")
            or len(summaries) != 1 or summaries[0].get("ok") is not True
            or summaries[0].get("items") != len(items) or summaries[0].get("results") != len(items)
            or len(results) != len(items) or len({row.get("id") for row in results}) != len(items)):
        raise BenchmarkError("Offline proof is incomplete, failed, or belongs to another backend/device.")
    by_id = {item["id"]: item for item in items}
    rows = []
    for result in results:
        item = by_id.get(result.get("id"))
        if (item is None or "error" in result or not isinstance(result.get("text"), str)
                or Path(result.get("wav", "")).resolve() != Path(item["wav"]).resolve()
                or result.get("expected_plan") != item["expected_plan"]
                or result.get("timing_mode") != "unpaced-batch"):
            raise BenchmarkError("Offline proof result does not match its current verified corpus item.")
        row = score(item, result, catalog)
        if result.get("plan") != row["plan"]:
            raise BenchmarkError("Offline proof parsed plan changed with current parser/catalog.")
        row["last_speech_to_final_ms"] = None
        row["release_to_final_ms"] = None
        rows.append(row)
    return {
        "version": 1, "backend": backend, "device": device, "subset": "synthetic", "timing": "batch",
        "source_proof": str(Path(path).resolve()), "source_proof_sha256": sha256(Path(path)),
        "cold_load_ms": summaries[0].get("cold_load_ms"), "results": rows,
        "summary": summarize(rows), "completed": True,
        "limitations": ["Synthetic accuracy is diagnostic and cannot select the personal-recording winner.",
                        "Original proof rows contain WAV paths but no per-WAV hash; current corpus hashes, paths, expected plans and parser results were checked.",
                        "Unpaced decode_ms is not end-of-speech latency."],
    }


def _save(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_benchmark(root: Path, backend: str, *, device="cpu", subset="mine", timing="realtime",
                  output: Path, resume=False, proof: Path | None = None, limit: int | None = None,
                  progress=None, sessions=None) -> dict:
    """Load once, checkpoint every utterance, and refuse incompatible resumes."""
    if subset not in {"mine", "synthetic"} or timing not in {"batch", "realtime"}:
        raise BenchmarkError("subset must be mine/synthetic; timing must be batch/realtime.")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise BenchmarkError("limit must be a positive integer.")
    if sessions is not None and subset != "mine":
        raise BenchmarkError("Session selection is only available for the mine subset.")
    root, output = Path(root).resolve(), Path(output).resolve()
    loaded = load_catalog(root)
    items, excluded = personal_items(root, loaded) if subset == "mine" else synthetic_items(root, loaded)
    items, excluded, selected_sessions = _select_sessions(root, items, excluded, sessions)
    if proof is not None:
        if subset != "synthetic" or timing != "batch" or resume or limit is not None:
            raise BenchmarkError("Proof reuse requires synthetic batch with no resume/limit.")
        report = reuse_offline_proof(proof, items, backend, device, loaded.catalog)
        _save(output, report)
        return report
    try:
        assets = model_assets(backend, device)
    except RuntimeError as error:
        raise BenchmarkError("Unknown benchmark backend.") from error
    if timing == "realtime" and "silero" not in assets:
        assets = (*assets, "silero")
    # Bind resume to inputs, implementation and model identity; no silent reuse
    # after a backend/config/dataset change. Deliberately omit the output path.
    module = ("moonshine" if backend.startswith("moonshine") else
              "whisper" if backend.startswith("whisper-") else backend)
    source_names = {"benchmark.py", "base.py", f"{module}.py", "__init__.py", "models.py"}
    if backend in ("qwen", "whisper-large", "whisper-turbo"):
        source_names.add("parakeet.py")
    if backend == "qwen":
        source_names.add("context.py")
    sources = [root / "voicebar" / "stt" / name for name in sorted(source_names)]
    sources.extend((root / "voicebar" / "parser.py", root / "voicebar" / "schema.py"))
    identity = {
        "backend": backend, "device": device, "subset": subset, "timing": timing,
        "runtime_packages": runtime_packages(),
        "selected_sessions": selected_sessions,
        "items": [{"id": item["id"], "wav_sha256": item["wav_sha256"], "expected_plan": item["expected_plan"],
                   "last_app": item.get("last_app"), "source": item["source"]} for item in items],
        "models_manifest_sha256": sha256(root / "models" / "manifest.json"),
        "implementation_sha256": {str(path.relative_to(root)): sha256(path) for path in sources},
        "reference": {"frame_samples": FRAME_SAMPLES, "onset": ONSET, "continuation": CONTINUATION,
                      "reset_after_silence_samples": REFERENCE_SILENCE_SAMPLES},
    }
    fingerprint = _json_hash(identity)
    if output.exists():
        if not resume:
            raise BenchmarkError("Output already exists; use --resume or choose a new output file.")
        report = _read_json(output)
        if report.get("fingerprint") != fingerprint:
            raise BenchmarkError("Resume inputs/backend/code/models differ from the checkpoint.")
    elif resume:
        raise BenchmarkError("Resume checkpoint does not exist.")
    else:
        report = {"version": 1, "backend": backend, "device": device, "subset": subset, "timing": timing,
                  "fingerprint": fingerprint, "identity": identity, "excluded_sessions": excluded,
                  "selected_sessions": selected_sessions,
                  "results": [], "loads": [], "completed": False,
                  "limitations": ["Speech end uses common Silero CPU estimates with 32 ms frame quantization, not manual annotations.",
                                   "Negative latency is retained and flagged; premature finalization is not counted as fast correct endpointing.",
                                   "Paced replay_elapsed_ms includes file duration; unpaced decode_ms is a separate measurement.",
                                   "First inference after each load is included and identified; cold model load is recorded separately."]}
    by_id = {item["id"]: item for item in items}
    completed = set()
    for row in report.get("results", []):
        identifier = row.get("id")
        if identifier not in by_id or identifier in completed or row.get("wav_sha256") != by_id[identifier]["wav_sha256"]:
            raise BenchmarkError("Resume checkpoint has duplicate or changed result identities.")
        completed.add(identifier)
    pending = [item for item in items if item["id"] not in completed]
    if not pending:
        report["completed"] = True
        report["summary"] = summarize(report["results"])
        _save(output, report)
        return report
    validating = time.perf_counter()
    directories = {name: require_model(root, name, verify_hashes=True) for name in assets}
    validation_ms = (time.perf_counter() - validating) * 1000
    vad = None
    if timing == "realtime":
        from .parakeet import Silero
        vad = Silero(directories["silero"])
    loading = time.perf_counter()
    recognizer = load_backend(backend, root, loaded.catalog, device=device)
    load = {"cold_load_ms": (time.perf_counter() - loading) * 1000,
            "asset_validation_ms": validation_ms, "assets": list(assets), "first_pending_id": pending[0]["id"]}
    load["runtime_details"] = dict(getattr(recognizer, "runtime_details", getattr(recognizer, "placement", {})))
    report["loads"].append(load)
    _save(output, report)
    try:
        for index, item in enumerate(pending[:limit] if limit is not None else pending):
            try:
                if timing == "realtime":
                    audio = read_wav(Path(item["wav"]))
                    reference = speech_end(audio, vad)
                    result = replay_paced(recognizer, audio, reference["speech_end_sample"])
                    result["speech_end_reference"] = reference
                else:
                    result = transcribe_wav(recognizer, Path(item["wav"]))
                row = score(item, result, loaded.catalog)
                row["runtime_details"] = dict(getattr(recognizer, "runtime_details", getattr(recognizer, "placement", {})))
                row["first_after_load"] = index == 0
            except (RuntimeError, OSError, ValueError, TypeError) as error:
                # Persist the failed item and stop. Reusing a recognizer after
                # an unknown native inference error would make later results unreliable.
                row = {"id": item["id"], "wav": item["wav"], "wav_sha256": item["wav_sha256"],
                       "condition": item["condition"], "error": str(error)}
                report["results"].append(row)
                report["summary"] = summarize(report["results"])
                _save(output, report)
                raise BenchmarkError(f"Benchmark stopped after saving failed item {item['id']}: {error}") from error
            report["results"].append(row)
            report["summary"] = summarize(report["results"])
            report["by_condition"] = {condition: summarize([row for row in report["results"] if row["condition"] == condition])
                                      for condition in sorted({row["condition"] for row in report["results"]})}
            report["completed"] = len(report["results"]) == len(items)
            _save(output, report)
            if progress is not None:
                progress(len(report["results"]), len(items), row)
    finally:
        recognizer.close()
    return report
