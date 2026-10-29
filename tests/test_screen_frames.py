"""Screen frames never leave RAM and cannot cross an authorization generation."""

import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from voicebar.screen_frames import MAX_FRAME_BYTES, ScreenFrame, ScreenFrames

PNG = b"\x89PNG\r\n\x1a\nowned-test-pixels"


@pytest.fixture
def frames():
    now = [10.0]
    store = ScreenFrames(clock=lambda: now[0])
    store.set_enabled(True, 3)
    return store, now, ScreenFrame(PNG, 101, 201, 10.0, 3)


def test_only_latest_frame_is_available_by_exact_identity(frames):
    store, now, frame = frames
    assert store.publish(frame)
    for hwnd, pid, generation in [(102, 201, 3), (101, 202, 3), (101, 201, 4)]:
        assert store.get(hwnd, pid, generation) is None
    now[0] = 11.0
    newer = replace(frame, png=PNG + b"new", captured_at=11.0)
    assert store.publish(newer)
    assert not store.publish(frame)
    assert store.get(101, 201, 3) is newer
    assert PNG.decode("latin1") not in repr(newer)
    with pytest.raises(FrozenInstanceError):
        newer.pid = 5


def test_expired_frame_is_cleared_and_clock_rollback_is_rejected(frames):
    store, now, frame = frames
    assert store.publish(frame)
    now[0] = 12.0
    assert store.get(101, 201, 3) is frame
    now[0] = 12.001
    assert store.get(101, 201, 3) is None
    now[0] = 10.0
    assert store.get(101, 201, 3) is None
    assert store.publish(frame)
    now[0] = 9.0
    assert store.get(101, 201, 3) is None
    assert not store.publish(frame)


@pytest.mark.parametrize("enabled, generation", [(False, 3), (True, 4), (False, 4)])
def test_disabling_or_changing_generation_drops_old_pixels(frames, enabled, generation):
    store, _, frame = frames
    assert store.publish(frame)
    store.set_enabled(enabled, generation)
    assert store.get(101, 201, 3) is None
    if not enabled or generation != 3:
        assert not store.publish(frame)


def test_late_gui_state_cannot_restore_an_older_generation(frames):
    store, _, frame = frames
    store.set_enabled(False, 4)
    store.set_enabled(True, 3)
    assert not store.publish(frame)
    store.set_enabled(True, 4)
    assert store.publish(replace(frame, generation=4))
    store.clear()
    assert store.get(101, 201, 4) is None


def test_inflight_capture_cannot_publish_after_revocation(frames):
    store, _, frame = frames
    ready, proceed = threading.Event(), threading.Event()
    published = []

    def producer():
        ready.set()
        assert proceed.wait(2)
        published.append(store.publish(frame))

    worker = threading.Thread(target=producer)
    worker.start()
    assert ready.wait(2)
    store.set_enabled(False, 4)
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
    assert published == [False]
    assert store.get(101, 201, 3) is None


@pytest.mark.parametrize("changes", [
    {"png": b"not a png"}, {"png": bytearray(PNG)}, {"png": PNG + b"x" * MAX_FRAME_BYTES},
    {"hwnd": 0}, {"pid": -1}, {"pid": True}, {"generation": True},
    {"captured_at": float("nan")}, {"captured_at": float("inf")}, {"captured_at": 11.0},
])
def test_malformed_or_unbounded_frames_are_refused(frames, changes):
    store, _, frame = frames
    assert not store.publish(replace(frame, **changes))
    assert store.get(101, 201, 3) is None


@pytest.mark.parametrize("age", [-1, True, float("nan"), float("inf"), "2"])
def test_frame_age_is_bounded_by_valid_monotonic_duration(frames, age):
    with pytest.raises(ValueError, match="age"):
        frames[0].get(101, 201, 3, max_age=age)
