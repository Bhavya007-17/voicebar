"""The live pipeline uses complete audio without streaming prefix work."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from voicebar import listening
from voicebar.catalog import Catalog, LoadedCatalog
from voicebar.gate import Gate
from voicebar.stt.base import SAMPLE_RATE, TextEvent


def test_listener_routes_whole_segment_to_final_only_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(listening, "_UNSAFE", threading.Event())
    stop = threading.Event()
    calls, rows = [], []
    loaded = LoadedCatalog(Catalog(apps=["Notepad"]), [], {}, [])

    def record(row):
        rows.append(row)
        if row.get("event") == "decision":
            stop.set()

    session = listening.GateSession(
        Gate(loaded.catalog, ["computer"]), execute=lambda _: pytest.fail("Executor called"),
        dry_run=True, log=record,
    )

    class Microphone:
        is_open = False

        @contextmanager
        def open(self, callback):
            self.is_open = True
            try:
                callback(bytes(1024), 512, "")
                yield {"name": "fake microphone"}
            finally:
                self.is_open = False

    class Segmenter:
        last_speech_at = None

        def reset(self):
            pass

        def feed(self, audio, start):
            self.last_speech_at = start + len(audio) / SAMPLE_RATE
            return [SimpleNamespace(audio=audio, start=start, end=self.last_speech_at,
                                    speech_end=self.last_speech_at)]

        def drain_drops(self):
            return []

    class Backend:
        def transcribe_segment(self, audio, emit):
            assert audio.dtype == np.float32 and len(audio) == 512
            calls.append("complete")
            emit(TextEvent("one", "ordinary conversation", True, 0, len(audio), 100, "fake"))

        def start(self, emit):
            pytest.fail("Listener invoked streaming prefix decoding")

        def close(self):
            calls.append("close")

    microphone = Microphone()
    listening.listen(
        tmp_path, loaded, backend_name="fake", backend=Backend(), microphone=microphone,
        segmenter=Segmenter(), session=session, stop=stop,
        controls=SimpleNamespace(available=lambda: True), output=lambda _: None,
    )
    assert calls == ["complete", "close"] and not microphone.is_open
    assert [row["reason"] for row in rows if row.get("event") == "decision"] == ["ignored"]
