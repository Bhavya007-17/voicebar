"""Exercise installed Moonshine listener exception handling without native models."""

from types import SimpleNamespace

import numpy as np
import pytest

from voicebar.stt.base import SttError
from voicebar.stt.moonshine import MoonshineBackend


@pytest.fixture
def fake_backend():
    from moonshine_voice.transcriber import (
        Error,
        LineCompleted,
        LineTextChanged,
        Stream,
    )

    class FakeStream:
        def __init__(self):
            self._listeners = []
            self._handle = 1
            self.closed = False
            self.partial = True
            self.engine_error = None

        def add_listener(self, listener):
            self._listeners.append(listener)

        def start(self):
            pass

        def report(self, event_type):
            line = SimpleNamespace(start_time=0.0, duration=0.1, line_id=0, text="open Chrome")
            # This is the installed package's real catcher: listener exceptions
            # would otherwise be swallowed, with no other listener to notify.
            Stream._emit(self, event_type(line=line, stream_handle=self._handle))

        def add_audio(self, audio, sample_rate):
            if self.partial:
                self.report(LineTextChanged)
                self.report(LineTextChanged)
            if self.engine_error is not None:
                Stream._emit(self, Error(error=self.engine_error, stream_handle=self._handle))

        def stop(self):
            self.report(LineCompleted)

        def close(self):
            self.closed = True
            self._listeners.clear()

    class FakeTranscriber:
        closed = False

        def create_stream(self, **kwargs):
            self.stream = FakeStream()
            return self.stream

        def close(self):
            self.closed = True

    backend = object.__new__(MoonshineBackend)
    backend.transcriber = FakeTranscriber()
    backend.stream = None
    backend.error = None
    yield backend
    backend.close()


def test_partial_callback_failure_propagates_and_preserves_first_error(fake_backend, capsys):
    first = ValueError("consumer rejected partial")
    calls = []

    def reject(event):
        calls.append(event)
        raise first

    fake_backend.start(reject)
    fake_backend.stream.engine_error = RuntimeError("later engine failure")
    with pytest.raises(SttError, match="consumer rejected partial") as failure:
        fake_backend.feed(np.zeros(512, dtype=np.float32))
    assert failure.value.__cause__ is first
    assert fake_backend.error is first
    assert len(calls) == 1
    assert "Exception in TranscriberEventListener" not in capsys.readouterr().err


def test_final_callback_failure_propagates_and_closes_stream(fake_backend):
    first = RuntimeError("consumer rejected final")

    def reject(event):
        raise first

    fake_backend.start(reject)
    stream = fake_backend.stream
    stream.partial = False
    fake_backend.feed(np.zeros(512, dtype=np.float32))
    with pytest.raises(SttError, match="consumer rejected final") as failure:
        fake_backend.finish()
    assert failure.value.__cause__ is first
    assert stream.closed
    assert fake_backend.stream is None


def test_native_error_is_not_replaced_by_later_callback(fake_backend):
    first = RuntimeError("native first failure")
    events = []
    fake_backend.start(events.append)
    stream = fake_backend.stream
    stream.partial = False
    stream.engine_error = first
    with pytest.raises(SttError, match="native first failure"):
        fake_backend.feed(np.zeros(512, dtype=np.float32))
    with pytest.raises(SttError, match="native first failure"):
        fake_backend.finish()
    assert events == []
    assert stream.closed


def test_falsey_exception_still_propagates(fake_backend):
    class FalseyError(ValueError):
        def __bool__(self):
            return False

    first = FalseyError("falsey callback failure")

    def reject(event):
        raise first

    fake_backend.start(reject)
    with pytest.raises(SttError, match="falsey callback failure"):
        fake_backend.feed(np.zeros(512, dtype=np.float32))


def test_valid_callbacks_emit_partials_and_final_then_reuse_clears_errors(fake_backend):
    events = []
    fake_backend.start(events.append)
    fake_backend.feed(np.zeros(512, dtype=np.float32))
    fake_backend.finish()
    assert [event.final for event in events] == [False, False, True]
    fake_backend.error = ValueError("old utterance failure")
    fake_backend.start(events.append)
    assert fake_backend.error is None
    fake_backend.finish()
    assert len(events) == 4
