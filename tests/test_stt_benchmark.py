"""Benchmark metric and provenance tests, without a model or audio device."""

import copy
import json

import numpy as np
import pytest

from voicebar.catalog import Catalog, InstalledApp, LoadedCatalog
from voicebar.stt import benchmark as bench
from voicebar.stt.base import TextEvent
from voicebar.stt.corpus import write_empty_utterance


def loaded():
    catalog = Catalog(apps=["Google Chrome", "Notepad"], aliases={"browser": "Google Chrome"})
    return LoadedCatalog(catalog, [InstalledApp(name, name) for name in catalog.apps], {}, [])


def prompt_set():
    return [{"id": f"p{number:03d}", "expected_text": "open Google Chrome",
             "expected_plan": [{"action": "open_app", "app": "Google Chrome"}],
             "source_index": 0, "source_text": "open chrome"} for number in range(1, 41)]


def session(root, *, label="quiet", complete=True):
    directory = root / "fixtures" / "mine" / f"session-{label}"
    directory.mkdir(parents=True)
    prompts = prompt_set()
    for entry in prompts:
        entry.update(status="recorded" if complete else "pending", attempts=[])
        if complete:
            filename = f"{entry['id']}-take-{'a' * 32}.wav"
            write_empty_utterance(directory / filename)
            entry["selected_wav"] = filename
            entry["attempts"] = [
                # Unselected failed/retried takes are not read or benchmarked.
                {"decision": "retry", "wav": "discarded.wav", "quality": {"accepted": False}},
                {"decision": "keep", "wav": filename, "wav_sha256": bench.sha256(directory / filename),
                 "quality": {"accepted": True, "frames": 1600}},
            ]
    manifest = {"version": 1, "completed": complete, "session_label": label,
                "audio_format": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
                "provenance": {"fixture": "locked"}, "prompts": prompts}
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


@pytest.fixture
def recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "build_prompts", lambda *_: (prompt_set(), {"fixture": "locked"}))
    quiet = session(tmp_path)
    noise = session(tmp_path, label="noise")
    session(tmp_path, label="abandoned", complete=False)
    return tmp_path, quiet, noise


def test_personal_bridge_uses_exactly_selected_completed_takes(recordings):
    root, _, _ = recordings
    items, excluded = bench.personal_items(root, loaded())
    assert len(items) == 80 and len({item["id"] for item in items}) == 80
    assert {item["condition"] for item in items} == {"quiet", "noise"}
    assert len(excluded) == 1 and "abandoned" in excluded[0]["manifest"]
    assert all(item["wav"].endswith("a" * 32 + ".wav") for item in items)
    assert all(item["source"]["provenance"] == {"fixture": "locked"} for item in items)


@pytest.mark.parametrize("change", ["hash", "expected_plan", "provenance", "accepted", "selected_path", "frame_count"])
def test_personal_bridge_refuses_changed_selected_data(recordings, change):
    root, (path, manifest), _ = recordings
    entry = manifest["prompts"][0]
    if change == "hash":
        entry["attempts"][-1]["wav_sha256"] = "0" * 64
    elif change == "expected_plan":
        entry["expected_plan"][0]["app"] = "Notepad"
    elif change == "provenance":
        manifest["provenance"] = {"fixture": "changed"}
    elif change == "accepted":
        entry["attempts"][-1]["quality"]["accepted"] = False
    elif change == "selected_path":
        entry["selected_wav"] = "../outside.wav"
    else:
        entry["attempts"][-1]["quality"]["frames"] += 1
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((bench.BenchmarkError, ValueError)):
        bench.personal_items(root, loaded())


class FakeVad:
    def __init__(self, probabilities):
        self.values = probabilities
        self.reset_count = 0
        self.frame_lengths = []

    def reset(self):
        self.reset_count += 1
        self.iterator = iter(self.values)

    def probability(self, frame):
        self.frame_lengths.append(len(frame))
        return next(self.iterator)


def test_common_speech_end_is_frame_estimate_and_resets_hysteresis():
    probabilities = [0.1, 0.6, 0.4] + [0.1] * 10 + [0.4, 0.7, 0.4, 0.1]
    audio = np.zeros(len(probabilities) * 512 - 7, dtype=np.float32)
    vad = FakeVad(probabilities)
    result = bench.speech_end(audio, vad)
    assert vad.reset_count == 1 and set(vad.frame_lengths) == {512}
    assert result["speech_end_sample"] == 16 * 512
    assert result["frame_resolution_ms"] == 32
    assert result["reset_after_silence_samples"] == 4800
    assert bench.speech_end(np.zeros(1024), FakeVad([0.4, 0.4]))["speech_end_sample"] is None


def test_common_speech_end_rejects_nonfinite_model_probability():
    with pytest.raises(bench.BenchmarkError, match="invalid probability"):
        bench.speech_end(np.zeros(512), FakeVad([float("nan")]))


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeBackend:
    name = "fake"
    device = "cpu"
    hotwords_supported = False
    partials_kind = "test"

    def __init__(self, clock, *, cost=0, early=False, no_final=False):
        self.clock, self.cost, self.early, self.no_final = clock, cost, early, no_final
        self.count = 0
        self.closed = False

    def start(self, emit):
        self.emit = emit

    def feed(self, audio):
        self.count += 1
        self.clock.now += self.cost
        if self.early and self.count == 1:
            self.emit(TextEvent("0", "open chrome", True, 0, 512, self.clock(), self.name))

    def finish(self):
        if not self.early and not self.no_final:
            self.emit(TextEvent("0", "open chrome", True, 0, 2048, self.clock(), self.name))

    def close(self):
        self.closed = True


def test_paced_backlog_counts_from_audio_clock_instead_of_feed_return():
    clock = Clock()
    backend = FakeBackend(clock, cost=0.1)
    result = bench.replay_paced(backend, np.zeros(2048), 512, clock=clock, sleep=clock.sleep)
    assert result["last_speech_to_final_ms"] == pytest.approx(400)
    assert result["release_to_final_ms"] == pytest.approx(304)
    assert result["replay_elapsed_ms"] == pytest.approx(432)
    assert result["decode_ms"] is None


def test_paced_early_final_is_negative_and_missing_final_has_no_latency():
    clock = Clock()
    early = bench.replay_paced(FakeBackend(clock, early=True), np.zeros(2048), 1536, clock=clock, sleep=clock.sleep)
    assert early["last_speech_to_final_ms"] == pytest.approx(-64)
    assert early["release_to_final_ms"] == pytest.approx(-96)
    clock = Clock()
    missing = bench.replay_paced(FakeBackend(clock, no_final=True), np.zeros(2048), 512, clock=clock, sleep=clock.sleep)
    assert missing["last_speech_to_final_ms"] is None
    assert missing["release_to_final_ms"] is None
    assert missing["text"] == ""


def item():
    return {"id": "sample", "text": "open chrome", "wav": "sample.wav", "wav_sha256": "a" * 64,
            "expected_plan": [{"action": "open_app", "app": "Google Chrome"}],
            "condition": "quiet", "source": {}, "last_app": None}


def test_exact_plan_scoring_does_not_normalize_payload_or_expected_targets():
    expected = item()
    untouched = copy.deepcopy(expected)
    wrong = bench.score(expected, {"text": "open Notepad"}, loaded().catalog)
    assert wrong["exact_plan"] is False
    assert wrong["app_error"] == {"kind": "wrong_app_target", "expected": ["Google Chrome"], "actual": ["Notepad"]}
    unknown = bench.score(expected, {"text": "open blargle"}, loaded().catalog)
    assert unknown["app_error"]["kind"] == "unresolved_with_app_target"
    assert expected == untouched
    expected["expected_plan"] = [{"action": "type_text", "text": "Hello", "submit": False}]
    assert bench.score(expected, {"text": "type hello"}, loaded().catalog)["exact_plan"] is False


def test_percentiles_and_timing_modes_preserve_missing_and_early_cases():
    rows = [
        {"exact_plan": True, "timing_mode": "realtime-replay", "last_speech_to_final_ms": value,
         "release_to_final_ms": value - 100 if value is not None else None, "decode_ms": 999}
        for value in [-20, 30, 300, None]
    ]
    rows.append({"exact_plan": False, "timing_mode": "unpaced-batch", "decode_ms": 12,
                 "last_speech_to_final_ms": 1})
    rows.append({"error": "failed"})
    summary = bench.summarize(rows)
    assert summary["exact_plan_accuracy"] == 4 / 6
    assert summary["last_speech_to_final_ms"] == {"count": 3, "p50": 30, "p95": 300}
    assert summary["unpaced_decode_ms"] == {"count": 1, "p50": 12, "p95": 12}
    assert summary["early_finals_before_estimated_speech_end"] == 1
    assert summary["missing_speech_end_or_final"] == 1


def proof_fixture(tmp_path):
    expected = item()
    expected["wav"] = str(tmp_path / "sample.wav")
    result = {"kind": "result", "id": "sample", "wav": expected["wav"], "text": "open chrome",
              "plan": expected["expected_plan"], "expected_plan": expected["expected_plan"],
              "timing_mode": "unpaced-batch", "decode_ms": 12}
    proof = {"ok": True, "backend": "moonshine", "device": "cpu", "returncode": 0,
             "ready_before_load": True, "violations": [], "audit_runtime_attempts": [],
             "records": [result, {"kind": "summary", "ok": True, "items": 1, "results": 1, "cold_load_ms": 20}]}
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(proof), encoding="utf-8")
    return path, expected, proof


def test_proof_reuse_checks_current_plan_and_never_invents_eos(tmp_path):
    path, expected, _ = proof_fixture(tmp_path)
    report = bench.reuse_offline_proof(path, [expected], "moonshine", "cpu", loaded().catalog)
    assert report["summary"]["exact_plan_accuracy"] == 1
    assert report["summary"]["last_speech_to_final_ms"]["count"] == 0
    assert report["results"][0]["release_to_final_ms"] is None
    assert report["cold_load_ms"] == 20


@pytest.mark.parametrize("change", ["device", "duplicate", "plan", "wav", "expected"])
def test_proof_reuse_refuses_stale_or_mismatched_results(tmp_path, change):
    path, expected, proof = proof_fixture(tmp_path)
    if change == "device":
        proof["device"] = "cuda"
    elif change == "duplicate":
        proof["records"].insert(0, proof["records"][0])
    elif change == "plan":
        proof["records"][0]["plan"] = None
    elif change == "wav":
        proof["records"][0]["wav"] = str(tmp_path / "other.wav")
    else:
        proof["records"][0]["expected_plan"] = None
    path.write_text(json.dumps(proof), encoding="utf-8")
    with pytest.raises(bench.BenchmarkError):
        bench.reuse_offline_proof(path, [expected], "moonshine", "cpu", loaded().catalog)


def test_resume_is_bound_to_inputs_and_preserves_completed_results(tmp_path, monkeypatch):
    items = [{**item(), "id": f"sample-{index}"} for index in range(3)]
    monkeypatch.setattr(bench, "load_catalog", lambda *_: loaded())
    monkeypatch.setattr(bench, "personal_items", lambda *_: (items, []))
    monkeypatch.setattr(bench, "sha256", lambda path: "a" * 64)
    monkeypatch.setattr(bench, "require_model", lambda *_, **__: tmp_path)
    created = []

    def backend(*_, device):
        created.append(device)
        return FakeBackend(Clock())

    monkeypatch.setattr(bench, "load_backend", backend)
    monkeypatch.setattr(bench, "transcribe_wav", lambda *_: {
        "text": "open chrome", "timing_mode": "unpaced-batch", "decode_ms": 1,
        "last_speech_to_final_ms": None, "release_to_final_ms": None,
    })
    output = tmp_path / "results.json"
    first = bench.run_benchmark(tmp_path, "parakeet", device="cuda", timing="batch", output=output, limit=1)
    saved = copy.deepcopy(first["results"][0])
    assert first["completed"] is False and len(first["results"]) == 1
    resumed = bench.run_benchmark(tmp_path, "parakeet", device="cuda", timing="batch", output=output, resume=True)
    assert resumed["completed"] and len(resumed["results"]) == 3
    assert resumed["results"][0] == saved and len(resumed["loads"]) == 2
    assert created == ["cuda", "cuda"]
    with pytest.raises(bench.BenchmarkError, match="differ"):
        bench.run_benchmark(tmp_path, "parakeet", device="cpu", timing="batch", output=output, resume=True)
    assert len(json.loads(output.read_text(encoding="utf-8"))["results"]) == 3


def fake_batch(monkeypatch, root):
    """Keep the real manifest/WAV bridge, replacing only models and inference."""
    monkeypatch.setattr(bench, "load_catalog", lambda *_: loaded())
    monkeypatch.setattr(bench, "sha256", lambda _: "a" * 64)
    monkeypatch.setattr(bench, "require_model", lambda *_, **__: root)
    monkeypatch.setattr(bench, "load_backend", lambda *_, **__: FakeBackend(Clock()))
    monkeypatch.setattr(bench, "transcribe_wav", lambda *_: {
        "text": "open Google Chrome", "timing_mode": "unpaced-batch", "decode_ms": 1,
        "last_speech_to_final_ms": None, "release_to_final_ms": None,
    })


def test_selected_repaired_cohort_excludes_both_old_sessions(recordings, monkeypatch):
    root, (quiet_path, _), (noise_path, _) = recordings
    session(root, label="check")
    original_manifests = {path: path.read_bytes() for path in (quiet_path, noise_path)}
    fake_batch(monkeypatch, root)
    report = bench.run_benchmark(root, "parakeet", timing="batch", output=root / "check.json",
                                 sessions=["session-check"])
    assert report["completed"] and len(report["results"]) == 40
    assert all(row["id"].startswith("session-check/") for row in report["results"])
    assert report["selected_sessions"] == report["identity"]["selected_sessions"] == ["session-check"]
    assert len(report["identity"]["items"]) == 40
    exclusions = {entry["manifest"].split("session-")[-1].split("\\")[0].split("/")[0]: entry["reason"]
                  for entry in report["excluded_sessions"]}
    assert exclusions == {"abandoned": "session is incomplete", "noise": "session was not selected",
                           "quiet": "session was not selected"}
    assert report["summary"]["exact_plan_accuracy"] == 1
    assert {path: path.read_bytes() for path in original_manifests} == original_manifests


@pytest.mark.parametrize(("sessions", "message"), [
    (["missing"], "Unknown session"),
    (["Session-quiet"], "Unknown session"),
    (["session-abandoned"], "not a complete validated"),
    (["session-quiet", "missing"], "Unknown session"),
    (["../session-quiet"], "exact directory name"),
    ([], "nonempty list"),
    ("session-quiet", "nonempty list"),
])
def test_requested_sessions_cannot_silently_disappear(recordings, monkeypatch, sessions, message):
    root, _, _ = recordings
    monkeypatch.setattr(bench, "load_catalog", lambda *_: loaded())
    with pytest.raises(bench.BenchmarkError, match=message):
        bench.run_benchmark(root, "parakeet", timing="batch", output=root / "invalid.json", sessions=sessions)
    assert not (root / "invalid.json").exists()


def test_session_selection_still_validates_unselected_provenance(recordings, monkeypatch):
    root, _, (noise_path, noise) = recordings
    noise["provenance"] = {"fixture": "changed"}
    noise_path.write_text(json.dumps(noise), encoding="utf-8")
    monkeypatch.setattr(bench, "load_catalog", lambda *_: loaded())
    with pytest.raises(bench.BenchmarkError, match="changed provenance"):
        bench.run_benchmark(root, "parakeet", timing="batch", output=root / "invalid.json",
                            sessions=["session-quiet"])


def test_resume_selection_is_bound_and_argument_order_is_irrelevant(recordings, monkeypatch):
    root, _, _ = recordings
    fake_batch(monkeypatch, root)
    output = root / "selected.json"
    first = bench.run_benchmark(root, "parakeet", timing="batch", output=output,
                                sessions=["session-quiet"], limit=1)
    saved = output.read_bytes()
    for selection in (["session-noise"], None):
        with pytest.raises(bench.BenchmarkError, match="differ"):
            bench.run_benchmark(root, "parakeet", timing="batch", output=output,
                                sessions=selection, resume=True)
        assert output.read_bytes() == saved
    resumed = bench.run_benchmark(root, "parakeet", timing="batch", output=output,
                                  sessions=["session-quiet", "session-quiet"], resume=True)
    assert resumed["completed"] and len(resumed["results"]) == 40
    assert resumed["results"][0] == first["results"][0]
    both = root / "both.json"
    bench.run_benchmark(root, "parakeet", timing="batch", output=both,
                        sessions=["session-quiet", "session-noise"], limit=1)
    reordered = bench.run_benchmark(root, "parakeet", timing="batch", output=both,
                                    sessions=["session-noise", "session-quiet"], resume=True, limit=1)
    assert reordered["selected_sessions"] == ["session-noise", "session-quiet"]


def test_no_selection_retains_all_complete_sessions(recordings, monkeypatch):
    root, _, _ = recordings
    fake_batch(monkeypatch, root)
    report = bench.run_benchmark(root, "parakeet", timing="batch", output=root / "all.json", limit=1)
    assert report["selected_sessions"] is None and report["identity"]["selected_sessions"] is None
    assert len(report["identity"]["items"]) == 80 and len(report["excluded_sessions"]) == 1


def test_resume_refuses_dependency_version_changes(recordings, monkeypatch):
    root, _, _ = recordings
    fake_batch(monkeypatch, root)
    monkeypatch.setattr(bench, "runtime_packages", lambda: {"torch": "first-version"})
    output = root / "package-bound.json"
    first = bench.run_benchmark(root, "qwen", timing="batch", output=output,
                                sessions=["session-quiet"], limit=1)
    assert first["identity"]["runtime_packages"] == {"torch": "first-version"}
    saved = output.read_bytes()
    monkeypatch.setattr(bench, "runtime_packages", lambda: {"torch": "changed-version"})
    with pytest.raises(bench.BenchmarkError, match="differ"):
        bench.run_benchmark(root, "qwen", timing="batch", output=output,
                            sessions=["session-quiet"], resume=True)
    assert output.read_bytes() == saved


def test_qwen_resume_refuses_changed_context_source(recordings, monkeypatch):
    from voicebar.stt.models import sha256 as real_sha256

    root, _, _ = recordings
    fake_batch(monkeypatch, root)
    source = root / "voicebar" / "stt" / "context.py"
    source.parent.mkdir(parents=True)
    source.write_text("CONTEXT_PROFILE = 'first'\n", encoding="utf-8")
    monkeypatch.setattr(bench, "sha256", lambda path: real_sha256(path) if path.name == "context.py" else "a" * 64)
    output = root / "context-bound.json"
    first = bench.run_benchmark(root, "qwen", timing="batch", output=output,
                                sessions=["session-quiet"], limit=1)
    hashes = {name.replace("\\", "/"): value for name, value in first["identity"]["implementation_sha256"].items()}
    assert hashes["voicebar/stt/context.py"] == real_sha256(source)
    saved = output.read_bytes()
    source.write_text("CONTEXT_PROFILE = 'changed'\n", encoding="utf-8")
    with pytest.raises(bench.BenchmarkError, match="differ"):
        bench.run_benchmark(root, "qwen", timing="batch", output=output,
                            sessions=["session-quiet"], resume=True)
    assert output.read_bytes() == saved


def test_synthetic_subset_rejects_personal_session_selection(tmp_path):
    with pytest.raises(bench.BenchmarkError, match="only available for the mine subset"):
        bench.run_benchmark(tmp_path, "parakeet", subset="synthetic", output=tmp_path / "invalid.json",
                            sessions=["session-quiet"])
