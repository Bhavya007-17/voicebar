"""Gate: decides whether heard text may run. Reference implementation: passes tests/test_gate_contract.py."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .catalog import Catalog
from .parser import parse, tokenize

SAFE_ACTIONS = {"open_app", "switch_app", "open_url", "nav", "scroll", "volume", "media"}
SAFE_WINDOW_OPS = {"maximize", "minimize", "restore", "move_monitor", "snap_left", "snap_right"}
SAFE_TAB_OPS = {"new", "next", "prev", "reopen"}
GREETING = {"hey", "ok", "okay"}


def is_safe(a: dict) -> bool:
    k = a["action"]
    if k in SAFE_ACTIONS:
        return True
    if k == "window":
        return a["op"] in SAFE_WINDOW_OPS
    if k == "tab":
        return a["op"] in SAFE_TAB_OPS
    if k == "mode":
        return a["op"] == "sleep"
    return False


@dataclass
class Decision:
    plan: list[dict] | None
    reason: str  # run | armed | ignored | unresolved | asleep


class Gate:
    def __init__(self, catalog: Catalog, names: list[str], mode: str = "name",
                 followup_s: float = 8.0, open_apps: list[str] | None = None):
        self.catalog = catalog
        self.names = {n.lower() for n in names}
        self.mode = mode
        self.followup_s = followup_s
        pinned = set(open_apps or [])
        self.open_catalog = dataclasses.replace(
            catalog,
            apps=[a for a in catalog.apps if a in pinned],
            aliases={k: v for k, v in catalog.aliases.items() if v in pinned},
        )
        self.until = float("-inf")
        self.last_app: str | None = None
        self.asleep = False
        self.dictating = False

    def _split_name(self, text: str) -> tuple[bool, str]:
        toks = tokenize(text)
        i = 0
        while i < len(toks) and toks[i].norm in GREETING:
            i += 1
        if i < len(toks) and toks[i].norm in self.names:
            return True, " ".join(t.raw for t in toks[i + 1:])
        return False, text

    def _accept(self, plan: list[dict], now: float) -> Decision:
        for a in plan:
            if a["action"] in ("open_app", "switch_app"):
                self.last_app = a["app"]
            elif a["action"] == "window" and a["target"] != "active":
                self.last_app = a["target"]
            elif a["action"] == "mode":
                self.asleep = a["op"] == "sleep" or (self.asleep and a["op"] != "wake")
                if a["op"] == "dictation_on":
                    self.dictating = True
                if a["op"] == "dictation_off":
                    self.dictating = False
        self.until = float("-inf") if self.asleep else now + self.followup_s
        return Decision(plan, "run")

    def hear(self, text: str, now: float) -> Decision:
        named, rest = self._split_name(text)

        if self.dictating:
            if parse(rest, self.catalog) == [{"action": "mode", "op": "dictation_off"}]:
                return self._accept([{"action": "mode", "op": "dictation_off"}], now)
            body = text.strip()
            if not body:
                return Decision(None, "ignored")
            return Decision([{"action": "type_text", "text": body + " ", "submit": False}], "run")

        if self.asleep:
            if named and parse(rest, self.catalog) == [{"action": "mode", "op": "wake"}]:
                return self._accept([{"action": "mode", "op": "wake"}], now)
            return Decision(None, "asleep")

        if named and not rest.strip():
            self.until = now + self.followup_s
            return Decision(None, "armed")

        if named or now <= self.until:
            plan = parse(rest, self.catalog, last_app=self.last_app)
            if plan is None:
                return Decision(None, "unresolved" if named else "ignored")
            return self._accept(plan, now)

        if self.mode != "open" or len(tokenize(rest)) < 2:
            return Decision(None, "ignored")
        plan = parse(rest, self.open_catalog, last_app=self.last_app)
        if plan is None or not all(is_safe(a) for a in plan):
            return Decision(None, "ignored")
        return self._accept(plan, now)
