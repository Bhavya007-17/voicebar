"""Input boundary tests use only fake APIs; no input or clipboard calls escape."""

import ctypes as C
from types import SimpleNamespace

import pytest

from voicebar.executor import input as module
from voicebar.executor.common import ExecutionError


class FakeBackend:
    def __init__(self):
        self.held = False
        self.events = []
        self.pastes = []
        self.muted = False
        self.level = 0.5

    def modifiers_held(self):
        return self.held

    def wheel_target(self, window):
        return True

    def send(self, events):
        self.events.append(events)
        if events[0][1] == 0xAD:
            self.muted = not self.muted
        elif events[0][1] == 0xAF:
            self.level += 0.02
        elif events[0][1] == 0xAE:
            self.level -= 0.02

    def paste(self, ctx, window, text):
        ctx.begin()
        self.pastes.append((window, text))

    def replace_text(self, ctx, window, text):
        return False

    def audio_state(self):
        return self.muted, self.level


@pytest.fixture
def ctx():
    window = SimpleNamespace(hwnd=10, pid=20, title="Owned Notepad")
    native = SimpleNamespace(active=10, elevated=False)
    native.foreground = lambda: native.active
    native.integrity = lambda pid: 0x3000 if native.elevated else 0x2000
    native.current_integrity = lambda: 0x2000
    context = SimpleNamespace(native=native, input_backend=FakeBackend(), settings={}, timeout=1,
                              starts=0, guards=[], window=window)

    def guard(target):
        context.guards.append(target)
        if native.elevated:
            raise ExecutionError("elevated")

    def begin():
        context.starts += 1

    def wait(predicate, description):
        result = predicate()
        if not result:
            raise ExecutionError(f"Unconfirmed: {description}")
        return result

    context.guard, context.begin, context.wait = guard, begin, wait
    context.target = lambda: window
    return context


def test_unicode_surrogate_pair_capitalization_and_punctuation(ctx):
    module.type_text(ctx, {"text": "A😀!?", "submit": False})
    events = [event for batch in ctx.input_backend.events for event in batch]
    assert [scan for kind, vk, scan, flags in events if flags == module.UNICODE] == [
        0x41, 0xD83D, 0xDE00, 0x21, 0x3F,
    ]
    assert all(vk == 0 and kind == 1 for kind, vk, scan, flags in events)
    assert len(events) == 10


def test_line_endings_become_enter_and_submit_is_separate(ctx):
    module.type_text(ctx, {"text": "A\r\nB\nC", "submit": True})
    batches = ctx.input_backend.events
    events = [event for batch in batches[:-1] for event in batch]
    assert sum(event[1] == 0x0D and event[3] == 0 for event in events) == 2
    assert batches[-1] == module.key_events("enter")


def test_chord_releases_in_reverse_and_extended_flag():
    events = module.key_events("ctrl+shift+t")
    assert [event[1] for event in events] == [0x11, 0x10, 0x54, 0x54, 0x10, 0x11]
    assert [event[3] for event in events] == [0, 0, 0, 2, 2, 2]
    assert module.key_events("alt+left")[1][3] == module.EXTENDED


@pytest.mark.parametrize("keys", ["f0", "f99", "f123", "ctrl+ctrl+s", "ctrl+shell"])
def test_unsupported_physical_keys_fail_before_input(ctx, keys):
    with pytest.raises(ExecutionError):
        module.press(ctx, {"keys": keys})
    assert not ctx.input_backend.events and ctx.starts == 0


@pytest.mark.parametrize("reason", ["held", "elevated", "foreground"])
def test_safety_guards_refuse_input(ctx, reason):
    if reason == "held":
        ctx.input_backend.held = True
    elif reason == "elevated":
        ctx.native.elevated = True
    else:
        ctx.native.active = 99
    with pytest.raises(ExecutionError):
        module.type_text(ctx, {"text": "Hello", "submit": False})
    assert not ctx.input_backend.events and ctx.starts == 0


@pytest.mark.parametrize("text", ["bad\0text", "bad\ud800text"])
def test_unrepresentable_text_fails_before_input(ctx, text):
    with pytest.raises(ExecutionError):
        module.type_text(ctx, {"text": text, "submit": False})
    assert not ctx.input_backend.events and ctx.starts == 0


def test_long_text_routes_to_paste_and_keeps_target_for_submit(ctx):
    ctx.settings["clipboard_threshold"] = 4
    module.type_text(ctx, {"text": "Long😀", "submit": True})
    assert ctx.input_backend.pastes == [(ctx.window, "Long😀")]
    assert ctx.input_backend.events == [module.key_events("enter")]


def test_partial_send_releases_only_own_inserted_keys():
    backend = module.InputBackend.__new__(module.InputBackend)
    calls = []

    def send(count, events, size):
        calls.append([(events[index].keyboard.vk, events[index].keyboard.flags) for index in range(count)])
        assert size == 40
        return 2 if len(calls) == 1 else count

    backend.send_input = send
    with pytest.raises(ExecutionError, match="2/4"):
        backend.send(module.key_events("ctrl+s"))
    assert calls[1] == [(0x53, 2), (0x11, 2)]


def test_scroll_signed_wheel_and_top(ctx):
    module.scroll(ctx, {"dir": "down", "amount": "large"})
    assert ctx.input_backend.events[0] == [(0, 0, -1080, 0x0800)]
    module.scroll(ctx, {"dir": "top"})
    assert ctx.input_backend.events[1] == module.key_events("ctrl+home")


def test_mute_and_unmute_are_idempotent_and_confirmed(ctx):
    module.volume(ctx, {"op": "unmute"})
    assert not ctx.input_backend.events
    module.volume(ctx, {"op": "mute"})
    module.volume(ctx, {"op": "mute"})
    module.volume(ctx, {"op": "unmute"})
    assert len(ctx.input_backend.events) == 2
    assert not ctx.input_backend.muted


def test_volume_direction_and_media_key(ctx):
    module.volume(ctx, {"op": "up"})
    assert ctx.input_backend.level > 0.5
    module.volume(ctx, {"op": "down"})
    assert ctx.input_backend.level == 0.5
    module.media(ctx, {"op": "next"})
    assert ctx.input_backend.events[-1] == [module.key_event(0xB0), module.key_event(0xB0, True)]


def test_wheel_refuses_to_route_to_another_window(ctx):
    ctx.input_backend.wheel_target = lambda window: False
    with pytest.raises(ExecutionError, match="Pointer is over another window"):
        module.scroll(ctx, {"dir": "down", "amount": "page"})
    assert ctx.input_backend.events == [] and ctx.starts == 0


class ClipboardFake:
    """HGLOBAL-like allocations are real Python buffers, never OS handles."""

    def __init__(self, formats):
        self.formats = dict(formats)
        self.buffers = {}
        self.next_handle = 1000
        self.sequence = 10
        self.empty_count = 0
        self.destroyed = False
        self.pasted = False
        self.user_copy = False
        self.unsupported_focus = False
        self.text = ""
        self.owner = 0
        self.open_owner = 0
        self.synthesize_on_close = False

    def CreateWindowExW(self, *args):
        return 900

    def DestroyWindow(self, owner):
        self.destroyed = True
        return True

    def OpenClipboard(self, owner):
        self.open_owner = owner
        return True

    def CloseClipboard(self):
        if self.synthesize_on_close and self.empty_count == 1 and 16 not in self.formats:
            self.formats[16] = 88
            self.sequence += 1
        return True

    def EmptyClipboard(self):
        self.empty_count += 1
        self.owner = self.open_owner
        self.formats.clear()
        self.sequence += 1
        return True

    def EnumClipboardFormats(self, previous):
        values = sorted(self.formats)
        return next((fmt for fmt in values if fmt > previous), 0)

    def GetClipboardData(self, fmt):
        return self.formats[fmt]

    def SetClipboardData(self, fmt, handle):
        self.formats[fmt] = handle
        self.sequence += 1
        return handle

    def GetClipboardSequenceNumber(self):
        return self.sequence

    def GetClipboardOwner(self):
        return self.owner

    def GlobalAlloc(self, flags, size):
        self.next_handle += 1
        self.buffers[self.next_handle] = C.create_string_buffer(size)
        return self.next_handle

    def GlobalLock(self, handle):
        return C.addressof(self.buffers[handle])

    def GlobalUnlock(self, handle):
        return True

    def GlobalFree(self, handle):
        self.buffers.pop(handle, None)

    def OleDuplicateData(self, handle, fmt, flags):
        return handle + 10000

    def CopyEnhMetaFileW(self, handle, path):
        return handle + 10000

    def DeleteEnhMetaFile(self, handle):
        return True

    def DeleteObject(self, handle):
        return True

    def SendMessageTimeoutW(self, *args):
        if args[1] == 0xB0:
            C.cast(args[2], C.POINTER(module.W.DWORD)).contents.value = 0
            C.cast(args[3], C.POINTER(module.W.DWORD)).contents.value = 0
            return 1
        raise AssertionError("Paste must use the application's keyboard command")

    def paste_keys(self, events):
        assert events == module.key_events("ctrl+v")
        self.pasted = True
        self.text = C.wstring_at(C.addressof(self.buffers[self.formats[13]]))
        if self.user_copy:
            self.formats = {13: 777}
            self.owner = 901
            self.sequence += 1
        return 1


def clipboard_backend(fake, ctx):
    backend = module.InputBackend.__new__(module.InputBackend)
    backend.user = backend.kernel = backend.ole = backend.gdi = fake
    backend._focus = lambda window: (11, window.pid)
    backend.modifiers_held = lambda: False
    backend.send = fake.paste_keys
    ctx.native.read_text = lambda hwnd: fake.text
    return backend


def test_clipboard_restores_every_supported_format_after_confirmed_paste(ctx):
    fake = ClipboardFake({13: 101, 8: 102, 14: 103, 0xC001: 104})
    clipboard_backend(fake, ctx).paste(ctx, ctx.window, "Long😀")
    assert fake.pasted and fake.destroyed
    assert fake.formats == {13: 10101, 8: 10102, 14: 10103, 0xC001: 10104}
    assert fake.empty_count == 2


@pytest.mark.parametrize("fmt", [0x80, 0x201, 0x301, 0x500])
def test_unknown_clipboard_formats_refuse_without_overwrite(ctx, fmt):
    fake = ClipboardFake({13: 101, fmt: 102})
    with pytest.raises(ExecutionError, match="cannot be safely preserved"):
        clipboard_backend(fake, ctx).paste(ctx, ctx.window, "Long")
    assert fake.formats == {13: 101, fmt: 102}
    assert fake.empty_count == 0 and not fake.pasted and fake.destroyed


def test_intervening_user_copy_wins_over_restore(ctx):
    fake = ClipboardFake({13: 101})
    fake.user_copy = True
    clipboard_backend(fake, ctx).paste(ctx, ctx.window, "Long")
    assert fake.formats == {13: 777}
    assert fake.empty_count == 1


def test_synthesized_formats_on_close_do_not_prevent_restore(ctx):
    fake = ClipboardFake({13: 101, 0xC001: 102})
    fake.synthesize_on_close = True
    clipboard_backend(fake, ctx).paste(ctx, ctx.window, "Long")
    assert fake.formats == {13: 10101, 0xC001: 10102}
    assert fake.empty_count == 2 and fake.destroyed


def test_unsupported_framework_refuses_before_clipboard_owner(ctx):
    fake = ClipboardFake({13: 101})
    backend = clipboard_backend(fake, ctx)

    def refuse(window):
        raise ExecutionError("unsupported framework")

    backend._focus = refuse
    with pytest.raises(ExecutionError, match="unsupported framework"):
        backend.paste(ctx, ctx.window, "Long")
    assert fake.empty_count == 0 and ctx.starts == 0


def replacing_backend(ctx, *, readonly=False):
    """A fake edit exposes UTF-16 selection positions and records EM_REPLACESEL."""
    backend = module.InputBackend.__new__(module.InputBackend)
    backend._focus = lambda window: (11, window.pid)
    ctx.native.is_window = lambda hwnd, pid: hwnd == 11 and pid == ctx.window.pid
    ctx.native.text = "A😀B"
    ctx.native.read_text = lambda hwnd: ctx.native.text
    calls = []

    def message(hwnd, msg, wparam, lparam, flags, timeout, result):
        calls.append((hwnd, msg, wparam))
        if msg == 0xB0:
            C.cast(wparam, C.POINTER(module.W.DWORD)).contents.value = 1
            C.cast(lparam, C.POINTER(module.W.DWORD)).contents.value = 3
        elif msg == 0xC2 and not readonly:
            replacement = C.wstring_at(lparam)
            assert replacement == "中"
            ctx.native.text = "A中B"
        return 1

    backend.user = SimpleNamespace(SendMessageTimeoutW=message)
    return backend, calls


def test_standard_edit_replaces_selected_surrogate_pair_and_preserves_undo(ctx):
    backend, calls = replacing_backend(ctx)
    assert backend.replace_text(ctx, ctx.window, "中")
    assert ctx.native.text == "A中B"
    assert calls == [(11, 0xB0, calls[0][2]), (11, 0xC2, 1)]
    assert ctx.starts == 1 and ctx.guards


def test_standard_edit_focus_change_refuses_before_replacement(ctx):
    backend, calls = replacing_backend(ctx)
    focuses = iter([(11, ctx.window.pid), (99, ctx.window.pid)])
    backend._focus = lambda window: next(focuses)
    with pytest.raises(ExecutionError, match="Focused edit control changed"):
        backend.replace_text(ctx, ctx.window, "中")
    assert all(message != 0xC2 for hwnd, message, wparam in calls)
    assert ctx.native.text == "A😀B" and ctx.starts == 0


def test_standard_edit_no_effect_is_not_reported_as_success(ctx):
    backend, calls = replacing_backend(ctx, readonly=True)
    with pytest.raises(ExecutionError, match="replacement text"):
        backend.replace_text(ctx, ctx.window, "中")
    assert calls[-1] == (11, 0xC2, 1)
    assert ctx.native.text == "A😀B"


def test_generic_framework_has_no_standard_edit_mutation(ctx):
    backend = module.InputBackend.__new__(module.InputBackend)

    def unsupported(window):
        raise module.UnsupportedEditControl("unsupported")

    backend._focus = unsupported
    assert backend.replace_text(ctx, ctx.window, "中") is False
    assert ctx.starts == 0
