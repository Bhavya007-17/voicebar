"""Gate lifecycle tests use fake clocks, capture, recognition and executors."""

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from voicebar.catalog import Catalog, CatalogError, LoadedCatalog
from voicebar.gate import Gate
from voicebar.listening import Chimes, DecisionLog, GateSession, configured_gate, listen


@pytest.fixture
def loaded():
    return LoadedCatalog(Catalog(apps=["Notepad", "Calculator"],
                                 engines={"google": "https://google.com/search?q={q}"}), [], {}, ["Notepad"])


def make_session(loaded, *, dry_run=False, execute=None, mode="name"):
    now, rows, calls = [0.0], [], []

    def run(plan):
        calls.extend(plan)
        now[0] += 30  # controlled slow completion, not a wall-clock delay
        return [{"ok": True}]

    session = GateSession(Gate(loaded.catalog, ["computer"], mode=mode, open_apps=loaded.pinned),
                          execute=execute or run, dry_run=dry_run, log=rows.append,
                          clock=lambda: now[0], chimes=Chimes(clock=lambda: now[0], play=lambda: None))
    return session, now, rows, calls


def hear(session, text, at=0):
    session.speech(at)
    return session.hear(text, start=at, end=at, speech_end=at, generation=session.generation)


def test_followup_restarts_when_slow_execution_finishes(loaded):
    session, now, _, calls = make_session(loaded)
    assert hear(session, "computer open notepad")["reason"] == "run"
    assert session.gate.until == 38
    now[0] = 37
    assert hear(session, "maximize it", 37)["reason"] == "run"
    assert calls[-1] == {"action": "window", "op": "maximize", "target": "Notepad"}


def test_failed_execution_does_not_arm_or_change_app(loaded):
    session, _, _, _ = make_session(loaded, execute=lambda p: [{"ok": False}])
    hear(session, "computer open notepad")
    assert session.gate.last_app is None
    assert hear(session, "close it", 1)["reason"] == "ignored"


def test_asleep_keeps_capture_open_and_only_named_wake_runs(loaded):
    session, now, rows, calls = make_session(loaded)
    assert hear(session, "computer go to sleep")["state_after"] == "asleep"
    assert session.capture_allowed
    now[0] = 1
    assert hear(session, "wake up", 1)["reason"] == "asleep"
    assert hear(session, "computer open notepad", 1)["reason"] == "asleep"
    assert hear(session, "computer wake up", 1)["state_after"] == "listening"
    assert calls == []  # modes belong to the session, never the native executor
    assert any(r.get("state") == "asleep" for r in rows)


def test_off_and_lock_preserve_prior_sleep_and_revoke_queued_speech(loaded):
    session, _, _, _ = make_session(loaded, dry_run=True)
    hear(session, "computer go to sleep")
    previous = session.generation
    session.control(locked=True)
    assert session.state == "locked" and not session.capture_allowed
    session.control(locked=False)
    assert session.state == "asleep" and session.capture_allowed
    assert session.hear("computer wake up", start=0, end=0, speech_end=0,
                        generation=previous)["reason"] == "stale_or_closed"
    session.control(enabled=False)
    assert session.state == "off" and not session.capture_allowed
    session.control(locked=True)
    session.control(locked=False)
    assert session.state == "off" and not session.capture_allowed


def test_dictation_times_out_after_sixty_seconds_without_speech(loaded):
    session, now, rows, calls = make_session(loaded)
    hear(session, "computer start dictation")
    now[0] = 59
    session.tick()
    assert session.gate.dictating
    session.speech(59)
    now[0] = 118.9
    session.tick()
    assert session.gate.dictating
    now[0] = 119
    session.tick()
    assert not session.gate.dictating
    assert any(r.get("reason") == "dictation_timeout" for r in rows)
    assert calls == []


def test_dictation_types_raw_text_and_stops_without_native_mode_action(loaded):
    session, now, _, calls = make_session(loaded)
    hear(session, "computer start dictation")
    now[0] = 1
    assert hear(session, "Hello, World!", 1)["reason"] == "run"
    assert calls == [{"action": "type_text", "text": "Hello, World! ", "submit": False}]
    now[0] = 32
    hear(session, "stop dictation", 32)
    assert not session.gate.dictating


def test_named_unresolved_routes_to_resolver_and_executes_valid_plan(loaded):
    session, _, _, calls = make_session(loaded)
    resolutions = []
    session.resolver = lambda *a: resolutions.append(a) or [{"action": "open_app", "app": "Notepad"}]
    assert hear(session, "ordinary talk")["reason"] == "ignored"
    assert hear(session, "computer something unknown")["reason"] == "run"
    assert len(resolutions) == 1
    assert calls == [{"action": "open_app", "app": "Notepad"}]
    assert resolutions[0][2]["names"] == {"computer"}


def test_chime_overlap_drops_entire_utterance_and_preserves_gate(loaded):
    session, now, _, calls = make_session(loaded)
    session.chimes.play = lambda: now.__setitem__(0, 2)
    session.chimes.cue()
    now[0] = 5
    result = session.hear("computer open notepad", start=1, end=4, speech_end=3,
                          generation=session.generation)
    assert result["reason"] == "chime_overlap" and not calls
    assert session.gate.last_app is None
    assert hear(session, "computer open notepad", 5)["reason"] == "run"


def test_dry_run_simulates_modes_and_followups_but_never_calls_executor(loaded):
    session, _, _, calls = make_session(loaded, dry_run=True, mode="open")
    assert hear(session, "open notepad")["reason"] == "run"
    assert hear(session, "type Hello", 1)["reason"] == "run"
    assert not calls and len(session.would_run) == 2
    assert all(r["dry_run"] for r in session.would_run)


def test_json_decisions_include_required_fields(tmp_path, loaded):
    log = DecisionLog(tmp_path)
    session, _, _, _ = make_session(loaded)
    session.log = log
    hear(session, "ordinary conversation")
    row = json.loads(log.path.read_text(encoding="utf-8"))
    assert {"time", "text", "mode", "state", "reason", "plan", "timings"} <= row.keys()


@pytest.mark.parametrize("value", ["names: []", "names: [some computer]", "names: [42]",
                                  "mode: other", "followup_seconds: .nan"])
def test_gate_configuration_fails_closed(tmp_path, loaded, value):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text(value, encoding="utf-8")
    with pytest.raises(CatalogError):
        configured_gate(tmp_path, loaded)


def test_gate_configuration_uses_exact_names_mode_followup_and_pinned(tmp_path, loaded):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text("names: [computer, computor]\nmode: open\nfollowup_seconds: 4\n", encoding="utf-8")
    gate = configured_gate(tmp_path, loaded)
    assert gate.names == {"computer", "computor"} and gate.followup_s == 4
    assert gate.hear("open calculator", 0).plan is None
    assert gate.hear("computor open calculator", 0).reason == "run"


def test_actual_stream_context_closes_on_lock_off_and_restores_on_unlock(tmp_path, loaded):
    session, _, rows, _ = make_session(loaded, dry_run=True)
    controls = SimpleNamespace(available=lambda: unlocked.is_set())
    unlocked, stop = threading.Event(), threading.Event()
    unlocked.set()
    transitions = __import__("queue").Queue()

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            transitions.put("opened")
            try:
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False
                transitions.put("closed")

    microphone = Microphone()
    backend = SimpleNamespace(close=lambda: None)
    segmenter = SimpleNamespace(reset=lambda: None)
    errors = []

    def worker():
        try:
            listen(tmp_path, loaded, backend_name="fake", backend=backend, microphone=microphone,
                   controls=controls, segmenter=segmenter, session=session, stop=stop, output=lambda x: None)
        except Exception as error:  # noqa: BLE001 - transfer test worker failures
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert transitions.get(timeout=3) == "opened"
        unlocked.clear()
        assert transitions.get(timeout=3) == "closed"
        assert session.state == "locked" and not microphone.is_open
        unlocked.set()
        assert transitions.get(timeout=3) == "opened"
        session.control(enabled=False)
        assert transitions.get(timeout=3) == "closed"
        assert session.state == "off" and not microphone.is_open
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert any(row.get("reason") == "stream_closed" for row in rows)
