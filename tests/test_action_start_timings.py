"""Native start receipts are separate from dispatch and completion."""

from voicebar.catalog import Catalog
from voicebar.gate import Gate
from voicebar.listening import GateSession


def test_native_start_receipt_measures_only_the_hop_after_gate():
    now = [105.0]

    def execute(plan):
        now[0] = 105.05
        return [{"ok": True, "action_started_at": 105.03}]

    session = GateSession(Gate(Catalog(apps=["Notepad"]), {"computer"}), execute=execute, clock=lambda: now[0])
    row = session.hear("computer open Notepad", start=104, end=104.8,
                       speech_end=104.7, generation=session.generation)
    assert row["reason"] == "run"
    assert round(row["timings"]["execute_start_ms"], 3) == 30
    assert round(row["timings"]["speech_end_to_action_start_ms"], 3) == 330
    assert round(row["timings"]["end_to_end_ms"], 3) == 350


def test_dispatch_is_not_reported_as_native_start_without_receipt():
    session = GateSession(Gate(Catalog(apps=["Notepad"]), {"computer"}),
                          execute=lambda plan: [{"ok": True}], clock=lambda: 10)
    row = session.hear("computer open Notepad", start=9, end=10,
                       speech_end=9.7, generation=session.generation)
    assert "gate_entry_to_dispatch_ms" in row["timings"]
    assert "execute_start_ms" not in row["timings"]
