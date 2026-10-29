"""Explicitly activated, temporary command sessions around the original gate.

This is opt-in. The reference gate and its eight-second contract stay intact.
Only a wake name starts a session; hearing speech can extend an existing one.
"""

from __future__ import annotations

import re

from .app_gate import Stage7Gate
from .gate import Decision
from .parser import parse, tokenize


class ConversationGate(Stage7Gate):
    conversational = True

    def _split_name(self, text):
        # Hesitations before the wake name are speech framing, not authorization.
        # Keep the original dictation text and every payload after a command verb.
        tokens = tokenize(text)
        index = 0
        while index < len(tokens) and tokens[index].norm in {"um", "uh", "ah", "erm", "er", "hey", "ok", "okay"}:
            index += 1
        if index < len(tokens) and tokens[index].norm in self.names:
            return True, " ".join(token.raw for token in tokens[index + 1:])
        return False, text

    def __init__(self, *args, silence_seconds=120.0, continuation_seconds=4.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.silence_seconds = silence_seconds
        self.continuation_seconds = continuation_seconds
        self.pending = None
        self.last_input = ""

    def reset_context(self):
        self.until = float("-inf")
        self.last_app = None
        self.pending = None

    def expire(self, now):
        if not self.dictating and now > self.until and self.until != float("-inf"):
            self.reset_context()
            return True
        return False

    def speech(self, at):
        if not self.asleep and not self.dictating and at <= self.until:
            self.until = max(self.until, at + self.silence_seconds)

    def _accept(self, plan, now):
        decision = super()._accept(plan, now)
        if self.asleep:
            self.reset_context()
        else:
            self.until = now + self.silence_seconds
        return decision

    def hear(self, text, now, *, active_app=None):
        self.expire(now)
        named, body = self._split_name(text)
        self.last_input = text
        if self.asleep or self.dictating:
            return super().hear(text, now, active_app=active_app)
        authorized = named or now <= self.until
        if not authorized:
            # A conversational session never starts from a nameless verb.
            return Decision(None, "ignored")
        self.until = now + self.silence_seconds
        key = " ".join(token.norm for token in tokenize(body))
        if key in {"cancel", "never mind", "nevermind", "forget that"}:
            self.pending = None
            return Decision(None, "cancelled")
        if named:
            self.pending = None
        if self.pending:
            prefix, deadline, app = self.pending
            self.pending = None
            # A complete new command replaces an unfinished request. No
            # unrelated command is silently typed/searched as its payload.
            if (now <= deadline and active_app == app
                    and parse(body, self.catalog, active_app=active_app) is None):
                body = prefix + " " + body.strip()
                self.last_input = body
                text = body
        # Search payloads are untargeted. A split typing request would need a
        # stable window/field identity across both segments, so do not retain it.
        incomplete = re.fullmatch(r"(?:search(?: up| for)?|look up)", key)
        engine_prefix = any(key == f"search {engine} for" for engine in self.catalog.engines)
        if incomplete or engine_prefix:
            self.pending = (body.strip().rstrip(".,;:"), now + self.continuation_seconds, active_app)
            return Decision(None, "pending")
        result = super().hear(text, now, active_app=active_app)
        if result.reason == "armed":
            self.until = now + self.silence_seconds
        elif result.reason == "ignored" and body.strip():
            return Decision(None, "unresolved")
        return result
