"""Fail-closed, deadline-bounded tier-2 selection over code-built actions."""

from __future__ import annotations

import json
import threading
import time
from typing import Protocol

from ..normalization import normalized_words
from ..parser import parse
from ..schema import validate_plan
from .candidates import (
    MAX_ACTIONS,
    ResolverRequest,
    build_request,
    context_value,
    strip_wake_name,
)
from .eligibility import eligible_request

MAX_OUTPUT_BYTES = 16 * 1024


class Backend(Protocol):
    def select(self, request: ResolverRequest, timeout_s: float) -> str | None:
        """Select candidate ids; transport/model errors may raise."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-JSON constant")


def decode_selection(output, request: ResolverRequest) -> list[dict] | None:
    """Decode only ids; ignore all model output that fails strict validation."""
    try:
        if (
            not isinstance(output, str)
            or not output.strip()
            or len(output) > MAX_OUTPUT_BYTES
        ):
            return None
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return None
        answer = json.loads(
            output, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        if not isinstance(answer, dict) or set(answer) != {"actions"}:
            return None
        identifiers = answer["actions"]
        if (
            not isinstance(identifiers, list)
            or not 1 <= len(identifiers) <= MAX_ACTIONS
        ):
            return None
        available = {candidate.id: candidate.action for candidate in request.candidates}
        if any(
            not isinstance(identifier, str)
            or identifier == "none"
            or identifier not in available
            for identifier in identifiers
        ):
            return None
        plan = [dict(available[identifier]) for identifier in identifiers]
        if len(plan) < request.minimum_actions:
            return None
        validate_plan(plan)
        return plan
    except Exception:  # noqa: BLE001 - malformed/untrusted output must never escape.
        return None


class Resolver:
    """One daemon worker at a time, including after a timed-out backend hangs.

    The caller never waits longer than its end-to-end deadline. A backend that
    ignores its timeout cannot accumulate unbounded workers or block shutdown.
    """

    def __init__(self, backend: Backend, timeout_ms: float = 1500):
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, (int, float))
            or not 0 < timeout_ms <= 60_000
        ):
            raise ValueError("timeout_ms must be positive and no greater than 60000")
        self.backend = backend
        self.timeout_s = timeout_ms / 1000
        self._state = threading.Condition()
        self._busy = False
        self.last_outcome = "not_started"

    def wait_idle(self, timeout_s: float) -> bool:
        """Wait for the actual worker to finish, independently of its action deadline.

        This is for orderly benchmarking/teardown. It never revives a result
        discarded by ``resolve`` and does not extend its action deadline.
        """
        with self._state:
            return self._state.wait_for(lambda: not self._busy, timeout_s)

    def resolve(self, text: str, catalog, context=None) -> list[dict] | None:
        started = time.monotonic()
        deadline = started + self.timeout_s
        with self._state:
            if self._busy:
                self.last_outcome = "busy"
                return None
            self._busy = True
        done = threading.Event()
        results = []
        outcomes = []

        def work():
            try:
                rest = strip_wake_name(text, context)
                if not rest:
                    outcomes.append("skipped")
                    return
                forbidden = getattr(catalog, "noncommands", ())
                if normalized_words(rest) in {
                    normalized_words(phrase) for phrase in forbidden
                }:
                    outcomes.append("skipped")
                    return
                if (
                    parse(rest, catalog, last_app=context_value(context, "last_app"))
                    is not None
                ):
                    outcomes.append("skipped")
                    return
                if not eligible_request(rest):
                    outcomes.append("non_request")
                    return
                request = build_request(rest, catalog, context)
                budget = deadline - time.monotonic()
                if request is None or budget <= 0:
                    outcomes.append("no_candidates" if request is None else "timeout")
                    return
                # Keep a private request for validation. An adapter receives
                # its own copy and cannot rewrite code's executable choices.
                adapter_request = ResolverRequest(
                    request.text,
                    request.prompt,
                    json.loads(json.dumps(request.schema)),
                    tuple(
                        type(candidate)(
                            candidate.id, dict(candidate.action), dict(candidate.labels)
                        )
                        for candidate in request.candidates
                    ),
                    request.minimum_actions,
                )
                output = self.backend.select(adapter_request, budget)
                if time.monotonic() < deadline:
                    plan = decode_selection(output, request)
                    results.append(plan)
                    if plan is not None:
                        outcomes.append("resolved")
                    elif output is None:
                        outcomes.append("not_sure")
                    elif isinstance(output, str) and len(output) <= MAX_OUTPUT_BYTES:
                        # A valid explicit abstention is a model decision;
                        # malformed/unknown output remains visible as invalid.
                        try:
                            abstention = (
                                len(output.encode("utf-8")) <= MAX_OUTPUT_BYTES
                                and json.loads(
                                    output,
                                    object_pairs_hook=_unique_object,
                                    parse_constant=_invalid_constant,
                                ) == {"actions": ["none"]}
                            )
                        except (ValueError, TypeError, RecursionError):
                            abstention = False
                        outcomes.append("not_sure" if abstention else "invalid_output")
                    else:
                        outcomes.append("invalid_output")
                else:
                    outcomes.append("timeout")
            except TimeoutError:
                outcomes.append("timeout")
                return
            except Exception:  # noqa: BLE001 - backend failures have no executable fallback.
                outcomes.append("backend_error")
                return
            finally:
                with self._state:
                    self._busy = False
                    self._state.notify_all()
                done.set()

        try:
            threading.Thread(target=work, name="voicebar-resolver", daemon=True).start()
        except Exception:  # noqa: BLE001 - failure to start the worker means no action.
            with self._state:
                self._busy = False
                self._state.notify_all()
            self.last_outcome = "backend_error"
            return None
        if (
            not done.wait(max(0.0, deadline - time.monotonic()))
            or time.monotonic() >= deadline
        ):
            self.last_outcome = "timeout"
            return None
        self.last_outcome = outcomes[0] if outcomes else "not_sure"
        return results[0] if results else None
