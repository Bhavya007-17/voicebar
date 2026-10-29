"""Native Moonshine streaming, explicitly loaded from the setup manifest."""

import time

from .base import SAMPLE_RATE, Backend, SttError, TextEvent, keyterms, samples


class MoonshineBackend(Backend):
    name = "moonshine"
    hotwords_supported = True

    def __init__(self, directory, catalog, *, name="moonshine"):
        if name not in ("moonshine", "moonshine-medium"):
            raise SttError(f"Unknown Moonshine backend: {name}")
        from moonshine_voice import ModelArch, Transcriber
        architecture = ModelArch.MEDIUM_STREAMING if name == "moonshine-medium" else ModelArch.SMALL_STREAMING
        self.name = name
        self.transcriber = Transcriber(
            model_path=directory, model_arch=architecture, update_interval=0.1,
            options={"keyterm_boost": 2.0, "return_audio_data": "false"},
        )
        self.transcriber.set_keyterms(keyterms(catalog))
        self.runtime_details = {"architecture": architecture.name, "execution_provider": "CPU",
                                "update_interval_seconds": 0.1, "keyterm_boost": 2.0,
                                "thread_configuration": "native default; no override"}
        self.stream = None
        self.error = None

    def start(self, emit):
        if self.stream is not None:
            raise SttError("Finish the previous Moonshine utterance first.")
        from moonshine_voice.transcriber import TranscriptEventListener
        backend = self

        class Listener(TranscriptEventListener):
            def report(self, event, final):
                if backend.error is not None:
                    return
                try:
                    line = event.line
                    begin = round(line.start_time * SAMPLE_RATE)
                    end = round((line.start_time + line.duration) * SAMPLE_RATE)
                    emit(TextEvent(str(line.line_id), line.text, final, begin, end,
                                   time.perf_counter(), backend.name))
                except Exception as error:  # noqa: BLE001 - transfer across native listener catcher
                    # Moonshine swallows failures from its sole listener. Transfer
                    # the first failure to feed/finish before accepting any more events.
                    if backend.error is None:
                        backend.error = error

            def on_line_text_changed(self, event):
                self.report(event, False)

            def on_line_completed(self, event):
                self.report(event, True)

            def on_error(self, event):
                if backend.error is None:
                    backend.error = event.error

        self.error = None
        self.stream = self.transcriber.create_stream(update_interval=0.1)
        try:
            self.stream.add_listener(Listener())
            self.stream.start()
        except BaseException:
            self.stream.close()
            self.stream = None
            raise

    def feed(self, audio):
        if self.stream is None:
            raise SttError("Moonshine stream has not started.")
        self.stream.add_audio(samples(audio).tolist(), sample_rate=SAMPLE_RATE)
        self._raise_error()

    def _raise_error(self):
        if self.error is not None:
            raise SttError(f"Moonshine transcription failed: {self.error}") from self.error

    def finish(self):
        if self.stream is None:
            raise SttError("Moonshine stream has not started.")
        try:
            self.stream.stop()
            self._raise_error()
        finally:
            self.stream.close()
            self.stream = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.transcriber.close()
