"""One bounded, transient screen frame; no persistence or pixel logging."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from time import perf_counter

MAX_FRAME_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ScreenFrame:
    png: bytes = field(repr=False)
    hwnd: int
    pid: int
    captured_at: float
    generation: int


class ScreenFrames:
    def __init__(self, *, clock=perf_counter):
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled = False
        self._generation = -1
        self._frame: ScreenFrame | None = None

    def set_enabled(self, enabled: bool, generation: int) -> None:
        if type(enabled) is not bool or type(generation) is not int or generation < 0:
            raise ValueError("Screen capture needs a boolean state and nonnegative generation")
        with self._lock:
            # A late GUI tick must not restore an older authorization.
            if generation < self._generation:
                return
            if not enabled or generation != self._generation:
                self._frame = None
            self._generation, self._enabled = generation, enabled

    def clear(self) -> None:
        with self._lock:
            self._frame = None

    def publish(self, frame: ScreenFrame) -> bool:
        if (not isinstance(frame, ScreenFrame) or type(frame.png) is not bytes
                or not 8 <= len(frame.png) <= MAX_FRAME_BYTES
                or not frame.png.startswith(b"\x89PNG\r\n\x1a\n")
                or type(frame.hwnd) is not int or frame.hwnd <= 0
                or type(frame.pid) is not int or frame.pid <= 0
                or type(frame.generation) is not int or frame.generation < 0
                or isinstance(frame.captured_at, bool)
                or not isinstance(frame.captured_at, (int, float))
                or not math.isfinite(frame.captured_at)):
            return False
        with self._lock:
            if (not self._enabled or frame.generation != self._generation
                    or frame.captured_at > self._clock()):
                return False
            if self._frame is not None and frame.captured_at < self._frame.captured_at:
                return False
            self._frame = frame
            return True

    def get(self, hwnd: int, pid: int, generation: int, max_age: float = 2.0) -> ScreenFrame | None:
        if (isinstance(max_age, bool) or not isinstance(max_age, (int, float))
                or not math.isfinite(max_age) or max_age < 0):
            raise ValueError("Screen frame age must be finite and nonnegative")
        with self._lock:
            frame = self._frame
            if not self._enabled or frame is None:
                return None
            age = self._clock() - frame.captured_at
            if not 0 <= age <= max_age:
                self._frame = None
                return None
            if (frame.hwnd, frame.pid, frame.generation) != (hwnd, pid, generation):
                return None
            return frame
