"""Pure endpoint policy: a partial transcript may shorten silence, never act."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .catalog import Catalog, CatalogError
from .gate import GREETING
from .parser import parse, tokenize


@dataclass(frozen=True)
class EndpointSettings:
    silence_seconds: float = .8
    short_silence_seconds: float = .25
    adaptive: bool = False

    @classmethod
    def from_document(cls, document):
        value = document.get("audio", {})
        if not isinstance(value, dict):
            raise CatalogError("audio settings must be a mapping")
        long = value.get("silence_seconds", .8)
        short = value.get("short_silence_seconds", .25)
        adaptive = value.get("adaptive_endpoint", False)
        if (not isinstance(adaptive, bool) or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) for number in (long, short))
                or not 0 < short <= long <= 2):
            raise CatalogError("audio silence waits must satisfy 0 < short <= ordinary <= 2 seconds; "
                               "adaptive_endpoint must be true or false")
        return cls(long, short, adaptive)


@dataclass
class PartialPolicy:
    """Read one catalog snapshot; no I/O, gate mutation, or learned resolution.

    The capture owner updates these plain state fields on its audio worker.
    Unknown/ambiguous text keeps the long wait. A resolved chain must contain
    no free-text action anywhere; dictation always keeps the long wait.
    """

    catalog: Catalog
    names: tuple[str, ...] = ("computer",)
    dictating: bool = False
    last_app: str | None = None
    active_app: str | None = None

    def __call__(self, text: str) -> str:
        if self.dictating:
            return "protected"
        tokens = tokenize(text)
        index = 0
        while index < len(tokens) and tokens[index].norm in GREETING:
            index += 1
        if index < len(tokens) and tokens[index].norm in self.names:
            text = " ".join(token.raw for token in tokens[index + 1:])
        plan = parse(text, self.catalog, self.last_app, active_app=self.active_app)
        if plan is None:
            return "unknown"
        if any(action["action"] in ("type_text", "web_search")
               or action["action"] == "mode" and action["op"] == "dictation_on"
               for action in plan):
            return "protected"
        return "complete"
