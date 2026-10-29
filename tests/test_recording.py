"""Fixture recording tests use generated PCM and fake capture; never a microphone."""

from __future__ import annotations

import json
import struct
import threading
import wave
from pathlib import Path

import pytest

from voicebar import recording
from voicebar.catalog import Catalog, InstalledApp, LoadedCatalog
from voicebar.parser import parse
from voicebar.recording import (
    CaptureResult,
    DefaultMicrophone,
    RecordingError,
    audio_quality,
    build_prompts,
    record_set,
)

DEVICE = {"name": "Fake mic", "index": 4, "hostapi": "Fake Windows host", "selection": "system-default-input"}
GOOD = struct.pack("<h", 1500) * 8000
CONTRACT = Path(__file__).with_name("test_parser_contract.py")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / CONTRACT.name).write_bytes(CONTRACT.read_bytes())
    return tmp_path


@pytest.fixture
def loaded():
    names = ["Google Chrome", "Claude", "Autodesk Fusion", "Word", "Excel",
             "Calculator", "Notepad", "Visual Studio Code"]
    catalog = Catalog(
        apps=names, aliases={"browser": "Google Chrome"},
        sites={"youtube": "https://www.youtube.com", "github": "https://github.com"},
        engines={"google": "https://www.google.com/search?q={q}",
                 "youtube": "https://www.youtube.com/results?search_query={q}"},
    )
    return LoadedCatalog(catalog, [InstalledApp(name, f"id:{name}") for name in names], {}, [])


class FakeRecorder:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = 0
        self.device_queries = 0

    def device_info(self):
        self.device_queries += 1
        return DEVICE.copy()

    def capture(self, stop, max_seconds, *, ready, startup_timeout):
        self.calls += 1
        result = self.results.pop(0) if self.results else CaptureResult(GOOD, DEVICE.copy())
        if isinstance(result, Exception):
            raise result
        ready.set()
        assert stop.wait(1)
        return result


def run(root, loaded, commands, *, recorder=None, **kwargs):
    commands = iter(commands)
    return record_set(
        root, loaded, input_fn=lambda prompt: next(commands), output_fn=lambda text: None,
        recorder=recorder or FakeRecorder(), stop_fn=lambda done: None, **kwargs,
    )


def read_manifest(session):
    return json.loads((session.path / "manifest.json").read_text(encoding="utf-8"))


def test_40_prompts_are_deterministic_provenanced_and_match_real_catalog(root, loaded):
    prompts, provenance = build_prompts(root, loaded)
    assert (prompts, provenance) == build_prompts(root, loaded)
    assert len(prompts) == 40
    assert len({prompt["id"] for prompt in prompts}) == 40
    assert all(0 <= prompt["source_index"] < 117 for prompt in prompts)
    assert all(parse(prompt["expected_text"], loaded.catalog) == prompt["expected_plan"] for prompt in prompts)
    actions = {action["action"] for prompt in prompts for action in prompt["expected_plan"]}
    assert actions == {"open_app", "switch_app", "close_app", "window", "open_url",
                       "tab", "nav", "web_search", "type_text", "press", "scroll",
                       "volume", "media", "mode"}
    assert provenance["contract_sha256"] == recording._digest(CONTRACT.read_bytes())


def test_prompt_target_fallback_uses_only_unique_installed_names(root, loaded):
    loaded.installed_apps = [InstalledApp("Notepad", "notepad")]
    loaded.catalog.apps = ["Notepad"]
    loaded.catalog.aliases = {}
    loaded.catalog.sites = {}
    loaded.catalog.engines = {"google": "https://www.google.com/search?q={q}"}
    prompts, provenance = build_prompts(root, loaded)
    assert provenance["substitutions"]["fusion"] == "Notepad"
    for prompt in prompts:
        for action in prompt["expected_plan"]:
            if "app" in action:
                assert action["app"] == "Notepad"


def test_missing_or_changed_contract_sources_fail_plainly(root, loaded):
    path = root / "tests" / CONTRACT.name
    path.write_text("COMMANDS = []", encoding="utf-8")
    with pytest.raises(RecordingError, match="provenance"):
        build_prompts(root, loaded)


def test_no_reachable_unique_installed_app_is_an_error(root, loaded):
    loaded.installed_apps = []
    with pytest.raises(RecordingError, match="unique installed app"):
        build_prompts(root, loaded)


@pytest.mark.parametrize(("pcm", "flags", "reason"), [
    (b"", (), "empty"),
    (b"\x00", (), "incomplete"),
    (GOOD[:100], (), "shorter"),
    (b"\0\0" * 8000, (), "quiet"),
    (struct.pack("<h", 32767) * 8000, (), "clipped"),
    (GOOD, ("input overflow",), "lost"),
    (GOOD * 41, (), "frame limit"),
], ids=["empty", "incomplete", "short", "quiet", "clipped", "overflow", "too-long"])
def test_quality_rejects_empty_loss_clipping_and_invalid_audio(pcm, flags, reason):
    quality = audio_quality(CaptureResult(pcm, DEVICE, flags))
    assert not quality["accepted"]
    assert any(reason in message for message in quality["reasons"])


def test_quality_reports_signal_and_cap_without_claiming_transcription():
    quality = audio_quality(CaptureResult(GOOD, DEVICE, capped=True))
    assert quality["accepted"]
    assert quality["frames"] == 8000
    assert quality["duration_seconds"] == 0.5
    assert quality["rms"] == pytest.approx(1500 / 32768)
    assert quality["capped"]


def test_quit_does_not_capture_and_sessions_have_unique_directories(root, loaded):
    fake = FakeRecorder()
    first = run(root, loaded, ["q"], recorder=fake)
    second = run(root, loaded, ["q"])
    assert fake.calls == 0
    assert first.path != second.path
    assert first.path.parent == root / "fixtures" / "mine"
    assert not first.completed
    assert len(first.manifest["prompts"]) == 40


def test_keep_writes_pcm_wav_and_device_quality_expected_plan(root, loaded):
    session = run(root, loaded, ["", "", "q"])
    manifest = read_manifest(session)
    entry = manifest["prompts"][0]
    attempt = entry["attempts"][0]
    assert entry["status"] == "recorded"
    assert entry["selected_wav"] == attempt["wav"]
    assert attempt["decision"] == "keep"
    assert attempt["device"] == DEVICE
    assert attempt["quality"]["accepted"]
    assert entry["expected_text"] == "open Google Chrome"
    assert entry["expected_plan"] == [{"action": "open_app", "app": "Google Chrome"}]
    with wave.open(str(session.path / attempt["wav"]), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.readframes(wav.getnframes()) == GOOD


def test_retry_retains_provenance_and_never_overwrites_the_first_take(root, loaded):
    fake = FakeRecorder([CaptureResult(GOOD, DEVICE), CaptureResult(GOOD * 2, DEVICE)])
    session = run(root, loaded, ["", "r", "", "", "q"], recorder=fake)
    entry = session.manifest["prompts"][0]
    assert [attempt["decision"] for attempt in entry["attempts"]] == ["retry", "keep"]
    assert entry["attempts"][0]["wav"] != entry["attempts"][1]["wav"]
    assert len(list(session.path.glob("*.wav"))) == 2


def test_bad_take_cannot_be_kept_and_empty_take_writes_no_wav(root, loaded):
    fake = FakeRecorder([CaptureResult(b"", DEVICE)])
    session = run(root, loaded, ["", "", "q"], recorder=fake)
    entry = session.manifest["prompts"][0]
    assert entry["status"] == "pending"
    assert entry["attempts"][0]["decision"] == "retry"
    assert entry["attempts"][0]["wav"] is None
    assert not list(session.path.glob("*.wav"))


def test_capture_failure_is_saved_and_prompt_can_be_skipped(root, loaded):
    fake = FakeRecorder([RuntimeError("lost device")])
    session = run(root, loaded, ["", "s", "q"], recorder=fake)
    entry = read_manifest(session)["prompts"][0]
    assert entry["status"] == "skipped"
    assert entry["attempts"][0]["error"] == "lost device"


def test_resume_preserves_recorded_takes_and_skips_without_capture(root, loaded):
    first = run(root, loaded, ["", "", "q"])
    fake = FakeRecorder()
    resumed = run(root, loaded, ["s", "q"], session_dir=first.path, recorder=fake)
    assert resumed.path == first.path
    assert fake.calls == 0
    assert resumed.manifest["prompts"][0] == first.manifest["prompts"][0]
    assert resumed.manifest["prompts"][1]["status"] == "skipped"
    assert resumed.manifest["prompts"][2]["status"] == "pending"


def test_all_40_accepted_is_complete_and_all_skips_is_not(root, loaded):
    complete = run(root, loaded, [item for _ in range(40) for item in ("", "")])
    skipped = run(root, loaded, ["s"] * 40)
    assert complete.completed
    assert read_manifest(complete)["completed"]
    assert not skipped.completed
    assert all(entry["status"] == "skipped" for entry in skipped.manifest["prompts"])


@pytest.mark.parametrize("mutation", ["catalog", "contract", "expected_plan", "wav"])
def test_resume_rejects_changed_provenance_and_audio(root, loaded, mutation):
    first = run(root, loaded, ["", "", "q"])
    if mutation == "catalog":
        loaded.catalog.aliases["new alias"] = "Notepad"
    elif mutation == "contract":
        contract = root / "tests" / CONTRACT.name
        contract.write_bytes(contract.read_bytes() + b"\n# changed\n")
    elif mutation == "expected_plan":
        manifest = read_manifest(first)
        manifest["prompts"][0]["expected_plan"][0]["app"] = "Notepad"
        (first.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        wav = first.path / first.manifest["prompts"][0]["selected_wav"]
        wav.write_bytes(wav.read_bytes()[:-2] + b"\0\0")
    with pytest.raises(RecordingError, match="resume"):
        run(root, loaded, ["q"], session_dir=first.path)


def test_resume_rejects_wav_traversal_and_external_directory(root, loaded, tmp_path):
    first = run(root, loaded, ["", "", "q"])
    manifest = read_manifest(first)
    manifest["prompts"][0]["attempts"][0]["wav"] = "../outside.wav"
    (first.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecordingError, match="WAV path"):
        run(root, loaded, ["q"], session_dir=first.path)
    with pytest.raises(RecordingError, match="direct session"):
        run(root, loaded, ["q"], session_dir=tmp_path)


def test_atomic_json_failure_preserves_previous_manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    recording._atomic_json(path, {"before": True})
    def fail(source, target):
        raise OSError("replace failed")
    monkeypatch.setattr(recording.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        recording._atomic_json(path, {"after": True})
    assert json.loads(path.read_text()) == {"before": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_wav_saved_before_failed_manifest_commit_is_not_selected(root, loaded, monkeypatch):
    original = recording._atomic_json
    commits = 0
    def fail_second(path, value):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("manifest disk failure")
        original(path, value)
    monkeypatch.setattr(recording, "_atomic_json", fail_second)
    with pytest.raises(OSError, match="manifest disk"):
        run(root, loaded, [""])
    path = next((root / "fixtures" / "mine").iterdir())
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["prompts"][0]["status"] == "pending"
    assert manifest["prompts"][0]["attempts"] == []
    assert len(list(path.glob("*.wav"))) == 1  # recoverable orphan; never a selected take


def test_keyboard_interrupt_closes_capture_and_preserves_resumable_manifest(root, loaded):
    class WaitingRecorder(FakeRecorder):
        closed = False
        def capture(self, stop, max_seconds, *, ready, startup_timeout):
            ready.set()
            assert stop.wait(1)
            self.closed = True
            return CaptureResult(GOOD, DEVICE)
    fake = WaitingRecorder()
    def interrupt(done):
        raise KeyboardInterrupt
    session = record_set(root, loaded, input_fn=lambda prompt: "", output_fn=lambda text: None,
                         recorder=fake, stop_fn=interrupt)
    assert fake.closed
    assert not session.completed
    assert read_manifest(session)["prompts"][0]["status"] == "pending"


@pytest.mark.parametrize("label", ["../outside", "a/b", "", "x" * 33])
def test_invalid_session_label_is_rejected(root, loaded, label):
    with pytest.raises(RecordingError, match="Session label"):
        run(root, loaded, ["q"], session_label=label)


@pytest.mark.parametrize("cap", [0, -1, 21, float("nan"), float("inf")])
def test_finite_20_second_cap_is_enforced(root, loaded, cap):
    with pytest.raises(RecordingError, match="cap"):
        run(root, loaded, ["q"], max_seconds=cap)


def test_raw_adapter_pins_format_default_device_closes_and_tracks_overflow():
    class FakeSD:
        opened = False
        closed = False
        settings = None
        def query_devices(self, *, kind):
            assert kind == "input"
            return {"index": 9, "name": "Default array", "hostapi": 1, "default_samplerate": 48000}
        def query_hostapis(self, index):
            assert index == 1
            return {"name": "WASAPI"}
        def check_input_settings(self, **kwargs):
            self.settings = kwargs
        def RawInputStream(self, **kwargs):
            owner = self
            class Stream:
                def __enter__(self):
                    owner.opened = True
                    assert kwargs["samplerate"] == 16000
                    assert kwargs["dtype"] == "int16" and kwargs["channels"] == 1
                    kwargs["callback"](GOOD, 8000, None, "input overflow")
                    return self
                def __exit__(self, *args):
                    owner.closed = True
            return Stream()
    fake = FakeSD()
    recorder = DefaultMicrophone(fake)
    recorder.device_info()
    assert not fake.opened
    ready = threading.Event()
    result = recorder.capture(threading.Event(), 0.01, ready=ready)
    assert fake.opened and fake.closed
    assert fake.settings == {"device": 9, "channels": 1, "dtype": "int16", "samplerate": 16000}
    assert len(result.pcm16) == 160 * 2
    assert result.capped
    assert result.status_flags == ("input overflow",)
    assert result.device["name"] == "Default array"
    assert ready.is_set()
    assert result.readiness["method"] == "first_valid_input_frame"


def test_adapter_device_loss_has_plain_error():
    class LostSD:
        def query_devices(self, **kwargs):
            raise RuntimeError("no input endpoint")
    with pytest.raises(RecordingError, match="default microphone.*no input endpoint"):
        DefaultMicrophone(LostSD()).device_info()


def test_ready_cue_follows_first_frame_and_precedes_stop_reader(root, loaded):
    order = []
    closed = threading.Event()

    class ReadyRecorder(FakeRecorder):
        def capture(self, stop, max_seconds, *, ready, startup_timeout):
            order.append("stream_open")
            order.append("first_buffer_stored")
            ready.set()
            assert stop.wait(1)
            order.append("stream_closed")
            closed.set()
            return CaptureResult(GOOD, DEVICE, readiness={"method": "first_valid_input_frame"})

    commands = iter(["", "", "q"])

    def controls(prompt):
        command = next(commands)
        if "starts recording" in prompt:
            order.append("start_enter" if command == "" else "quit")
        else:
            assert closed.is_set()
        return command

    def output(text):
        if text.startswith("Ready"):
            assert "first_buffer_stored" in order and not closed.is_set()
            order.append("ready_cue")

    def stop_reader(done):
        assert order[-1] == "ready_cue"
        order.append("stop_reader")

    result = record_set(root, loaded, input_fn=controls, output_fn=output,
                        recorder=ReadyRecorder(), stop_fn=stop_reader)
    assert order[:6] == ["start_enter", "stream_open", "first_buffer_stored", "ready_cue", "stop_reader", "stream_closed"]
    assert result.manifest["prompts"][0]["attempts"][0]["readiness"]["method"] == "first_valid_input_frame"


def test_failed_startup_never_prints_ready_or_starts_stop_reader(root, loaded):
    output, stop_calls = [], []
    commands = iter(["", "s", "q"])
    result = record_set(root, loaded, input_fn=lambda _: next(commands), output_fn=output.append,
                        recorder=FakeRecorder([RuntimeError("open failed")]),
                        stop_fn=lambda _: stop_calls.append(True))
    assert not stop_calls
    assert not any(text.startswith("Ready") for text in output)
    assert result.manifest["prompts"][0]["attempts"][0]["error"] == "open failed"


def test_startup_timeout_stops_and_joins_without_consuming_stop_input():
    closed, stop_reader_called = threading.Event(), []

    class NeverReady:
        def capture(self, stop, max_seconds, *, ready, startup_timeout):
            assert stop.wait(1)
            closed.set()
            return CaptureResult(b"", DEVICE)

    with pytest.raises(RecordingError, match="startup timed out"):
        recording._capture(NeverReady(), 20, lambda _: stop_reader_called.append(True),
                           on_ready=lambda: pytest.fail("No Ready cue before audio"), startup_timeout=0.01)
    assert closed.is_set() and not stop_reader_called


def test_capture_that_finishes_without_readiness_is_rejected():
    class NoConfirmation:
        def capture(self, stop, max_seconds, *, ready, startup_timeout):
            return CaptureResult(GOOD, DEVICE)

    with pytest.raises(RecordingError, match="without confirming"):
        recording._capture(NoConfirmation(), 20, lambda _: pytest.fail("No stop reader"),
                           on_ready=lambda: pytest.fail("No Ready cue"))


def test_unclosed_worker_is_fatal_and_no_second_take_starts(root, loaded, monkeypatch):
    release, closed = threading.Event(), threading.Event()

    class HungCleanup(FakeRecorder):
        def capture(self, stop, max_seconds, *, ready, startup_timeout):
            self.calls += 1
            ready.set()
            release.wait(1)
            closed.set()
            return CaptureResult(GOOD, DEVICE)

    fake = HungCleanup()
    commands = iter(["", "", "q"])
    monkeypatch.setattr(recording, "_CLEANUP_TIMEOUT", 0.01)
    monkeypatch.setattr(recording, "_CAPTURE_CLEANUP_FAILED", False)
    try:
        with pytest.raises(recording.RecordingCleanupError, match="exit this process"):
            record_set(root, loaded, input_fn=lambda _: next(commands), output_fn=lambda _: None,
                       recorder=fake, stop_fn=lambda _: None)
        assert fake.calls == 1
        with pytest.raises(recording.RecordingCleanupError, match="previous microphone"):
            run(root, loaded, [""], recorder=fake)
        assert fake.calls == 1
        manifest = json.loads(next((root / "fixtures" / "mine").glob("*/manifest.json")).read_text(encoding="utf-8"))
        assert manifest["prompts"][0]["attempts"][0]["decision"] == "quit"
        assert not list((root / "fixtures" / "mine").glob("*/*.wav"))
    finally:
        release.set()
        assert closed.wait(1)


class FakeNativeInput:
    def __init__(self, on_open):
        self.on_open = on_open
        self.closed = False

    def query_devices(self, **kwargs):
        return {"index": 4, "name": "Fake native input", "hostapi": 0, "default_samplerate": 16000}

    def query_hostapis(self, index):
        return {"name": "Fake host"}

    def check_input_settings(self, **kwargs):
        pass

    def RawInputStream(self, **kwargs):
        owner = self

        class Stream:
            def __enter__(self):
                owner.on_open(kwargs["callback"])
                return self

            def __exit__(self, *args):
                owner.closed = True

        return Stream()


def test_native_adapter_waits_for_valid_frame_and_cap_excludes_startup(monkeypatch):
    ready = threading.Event()
    timestamps = iter([100.0, 107.0, 107.0])
    monkeypatch.setattr(recording.time, "monotonic", lambda: next(timestamps))
    waits = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)
            return True

    def on_open(callback):
        callback(b"", 0, None, None)
        assert not ready.is_set()
        callback(GOOD, 8000, None, None)
        assert ready.is_set()

    fake = FakeNativeInput(on_open)
    result = DefaultMicrophone(fake).capture(Stop(), 20, ready=ready, startup_timeout=10)
    assert waits == [20.0]  # seven seconds spent opening did not consume the cap
    assert result.pcm16 == GOOD and fake.closed
    assert result.readiness["startup_ms"] == 7000


def test_native_adapter_no_frame_timeout_closes_stream():
    fake = FakeNativeInput(lambda callback: None)
    ready = threading.Event()
    with pytest.raises(RecordingError, match="first audio frame"):
        DefaultMicrophone(fake).capture(threading.Event(), 20, ready=ready, startup_timeout=0.01)
    assert fake.closed and not ready.is_set()


def test_ready_output_flushes_console(monkeypatch):
    calls = []
    monkeypatch.setattr(recording, "print", lambda *args, **kwargs: calls.append((args, kwargs)), raising=False)
    recording._console_output("Ready — say the command.")
    assert calls == [(("Ready — say the command.",), {"flush": True})]


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf")])
def test_startup_timeout_is_finite_and_bounded(root, loaded, timeout):
    with pytest.raises(RecordingError, match="startup timeout"):
        run(root, loaded, ["q"], startup_timeout=timeout)
