"""A late executor continuation cannot pass a mutation or wait guard."""

import threading
from types import SimpleNamespace

import pytest

from voicebar.executor.common import Context, ExecutionError


@pytest.mark.parametrize("operation", ["begin", "guard", "wait"])
def test_cancelled_context_refuses_effects_and_wait_predicates(operation):
    context = object.__new__(Context)
    context.cancel = threading.Event()
    context.cancel.set()
    context.native = SimpleNamespace(is_window=lambda *args: pytest.fail("Native target was touched"),
                                     wait=lambda *args: pytest.fail("Native wait was started"))
    context.timeout = 30
    with pytest.raises(ExecutionError, match="timed out"):
        if operation == "guard":
            context.guard(SimpleNamespace(hwnd=10, pid=20))
        elif operation == "wait":
            context.wait(lambda: pytest.fail("Cancelled predicate ran"), "anything")
        else:
            context.begin()
