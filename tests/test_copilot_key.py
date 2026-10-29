"""Exact physical chord tests never inject keyboard input into Windows."""

from voicebar.ui.copilot import CopilotChord


def test_physical_copilot_toggles_once_per_press_and_consumes_only_f23():
    calls = []
    chord = CopilotChord(lambda: calls.append("toggle"))
    assert not chord.key(0xA0, True)
    assert not chord.key(0x5B, True)
    assert chord.key(0x86, True)
    assert chord.key(0x86, True)
    assert calls == ["toggle"]
    assert not chord.key(0x5B, False)
    assert not chord.key(0xA0, False)
    assert chord.key(0x86, False)
    assert not chord.key(0x41, True)
    chord.key(0xA0, True)
    chord.key(0x5B, True)
    assert chord.key(0x86, True)
    assert calls == ["toggle", "toggle"]


def test_plain_f23_other_chords_and_injected_input_pass_unchanged():
    calls = []
    chord = CopilotChord(lambda: calls.append(True))
    assert not chord.key(0x86, True)
    chord.key(0xA1, True)  # right Shift alone is not the physical Copilot chord
    chord.key(0x5B, True)
    assert not chord.key(0x86, True)
    chord.key(0xA0, True)
    assert not chord.key(0x86, True, injected=True)
    assert not chord.key(0x43, True)
    assert calls == []
