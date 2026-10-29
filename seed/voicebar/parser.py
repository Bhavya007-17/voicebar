"""Tier-1 command parser: transcript -> list of action dicts, or None.

Pure Python, no I/O, no model. Rules it enforces:
  * All or nothing: every word must be consumed by commands, else None.
  * Matching runs on a normalized copy; payloads (text to type, search
    queries) are cut from the raw transcript so case and punctuation survive.
  * type/search swallow the rest of the utterance.
  * App names resolve by alias, exact name, whole-token subset, then
    full-string similarity. Never substring-match a long phrase to a short name.
  * Ambiguous or unknown targets resolve to None.
Reference implementation: passes tests/test_parser_contract.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .catalog import Catalog

FUZZY_MIN = 85       # minimum full-string similarity for a near-miss app name
MARGIN = 5           # best candidate must beat the runner-up by this much
MAX_COMMAND_WORDS = 40


@dataclass
class Tok:
    raw: str    # as transcribed
    norm: str   # lowercased, edge punctuation stripped


@dataclass
class Ctx:
    last_app: str | None = None   # what "it" refers to


_EDGE = re.compile(r"^[^\w']+|[^\w']+$")


def tokenize(text: str) -> list[Tok]:
    out = []
    for raw in text.split():
        norm = _EDGE.sub("", raw.lower())
        if norm:
            out.append(Tok(raw, norm))
    return out


def _text(toks: list[Tok]) -> str:
    return " ".join(t.norm for t in toks)


def _payload(toks: list[Tok]) -> str:
    """Raw text for typing/searching: keeps case, inner punctuation, ? and !."""
    return " ".join(t.raw for t in toks).rstrip(" .,;:").lstrip(" ,;:")


LEAD = [
    ["hey"], ["ok"], ["okay"], ["please"], ["um"], ["uh"], ["now"], ["just"],
    ["can", "you"], ["could", "you"], ["would", "you"], ["will", "you"],
    ["go", "ahead", "and"],
]
TRAIL = [["please"], ["for", "me"], ["now"], ["real", "quick"], ["thanks"], ["thank", "you"]]


def _strip_politeness(toks: list[Tok]) -> list[Tok]:
    changed = True
    while changed and toks:
        changed = False
        norms = [t.norm for t in toks]
        for p in LEAD:
            if norms[: len(p)] == p and len(toks) > len(p):
                toks, changed = toks[len(p):], True
                break
        if changed:
            continue
        for p in TRAIL:
            if norms[-len(p):] == p and len(toks) > len(p):
                toks, changed = toks[: -len(p)], True
                break
    return toks


# ------------------------------------------------------------------ targets

ARTICLES = {"the", "my", "a", "an"}
NOISE_LEAD = {"new"}
NOISE_TRAIL = {"app", "application", "program", "window", "document", "browser"}
DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _squash(s: str) -> str:
    return s.lower().replace(" ", "")


def _exact(q: str, cat: Catalog):
    """Alias, app name, then site key. Space-insensitive ("you tube" == "youtube")."""
    sq = _squash(q)
    for k, v in cat.aliases.items():
        if _squash(k) == sq:
            return ("app", v)
    for a in cat.apps:
        if _squash(a) == sq:
            return ("app", a)
    for k in cat.sites:
        if _squash(k) == sq:
            return ("site", k)
    return None


def resolve(words: list[str], cat: Catalog):
    """-> ("app", name) | ("site", key) | ("url", url) | None if unknown or ambiguous."""
    while words and words[0] in ARTICLES:
        words = words[1:]
    if not words:
        return None
    q = " ".join(words)
    hit = _exact(q, cat)
    if hit:
        return hit
    if "dot" in words[1:]:                                  # "stripe dot com"
        dom = re.sub(r"\s*dot\s*", ".", q).replace(" ", "")
        if DOMAIN.match(dom):
            return ("url", "https://" + dom)
    if len(words) == 1 and DOMAIN.match(words[0]):          # "docs.python.org"
        return ("url", "https://" + words[0])

    core = list(words)                                      # "a new word document" -> "word"
    while core and core[0] in NOISE_LEAD:
        core = core[1:]
    while core and core[-1] in NOISE_TRAIL:
        core = core[:-1]
    if not core:
        return None
    if core != words:
        q = " ".join(core)
        hit = _exact(q, cat)
        if hit:
            return hit

    scored: list[tuple[float, str]] = []
    qset = set(core)
    for a in cat.apps:
        atoks = a.lower().split()
        if qset <= set(atoks):                              # "chrome" in "Google Chrome"
            scored.append((95 - 2 * (len(atoks) - len(qset)), a))
        else:
            r = fuzz.ratio(_squash(q), _squash(a))          # full string, never partial
            if r >= FUZZY_MIN:
                scored.append((r, a))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MARGIN:
        return None                                         # ambiguous: do nothing
    return ("app", scored[0][1])


# ------------------------------------------------------------------ grammar

PRONOUN = r"it|this|that|this window|that window|the window|window|this one"
MON_WORD = r"other|another|next|second|previous|first|left|right|main|primary|laptop"
MON_MAP = {
    "other": "next", "another": "next", "next": "next", "second": "next",
    "previous": "prev", "left": "left", "right": "right",
    "main": "primary", "primary": "primary", "laptop": "primary", "first": "primary",
}
MON_TAIL = re.compile(
    rf"^(?P<head>.+?) (?:to|on|onto) (?:the |my |an )?(?P<mon>{MON_WORD}) (?:screen|monitor|display)$")
TAB_TAIL = re.compile(
    r"^(?P<head>.+?) (?:in|on) (?:a |the |another )?(?:new )?(?:google |chrome |browser )?tab$")
BROWSER_TAIL = re.compile(
    r"^(?P<head>.+?) (?:in|on) (?:the |my )?(?:google chrome|chrome|browser|edge)$")

OPEN_VERB = r"open up|open|launch|start|run|pull up|bring up|show me|show"
SWITCH_VERB = r"switch over to|switch to|go to|focus on|focus|jump to"
CLOSE_VERB = r"close out of|close|exit|quit"
MOVE_VERB = r"move|send|put|throw|drag|bring"

FIXED: list[tuple[str, dict]] = [
    (r"stop listening|go to sleep|sleep", {"action": "mode", "op": "sleep"}),
    (r"wake up", {"action": "mode", "op": "wake"}),
    (r"start dictation|start dictating|dictation mode", {"action": "mode", "op": "dictation_on"}),
    (r"stop dictation|stop dictating|end dictation", {"action": "mode", "op": "dictation_off"}),
    (r"go back|back", {"action": "nav", "op": "back"}),
    (r"go forward|forward", {"action": "nav", "op": "forward"}),
    (r"(?:refresh|reload)(?: (?:the |this )?page)?", {"action": "nav", "op": "reload"}),
    (r"close (?:this |the |that |current )?tab", {"action": "tab", "op": "close"}),
    (r"next tab", {"action": "tab", "op": "next"}),
    (r"(?:previous|last) tab", {"action": "tab", "op": "prev"}),
    (r"reopen (?:(?:that|the|closed|last) )*tab", {"action": "tab", "op": "reopen"}),
    (r"select (?:all|everything)", {"action": "press", "keys": "ctrl+a"}),
    (r"copy(?: that| this| it)?", {"action": "press", "keys": "ctrl+c"}),
    (r"paste(?: that| this| it)?", {"action": "press", "keys": "ctrl+v"}),
    (r"cut(?: that| this| it)?", {"action": "press", "keys": "ctrl+x"}),
    (r"undo(?: that)?", {"action": "press", "keys": "ctrl+z"}),
    (r"redo(?: that)?", {"action": "press", "keys": "ctrl+y"}),
    (r"save(?: it| this| the file| the document)?", {"action": "press", "keys": "ctrl+s"}),
    (r"scroll to (?:the )?top|(?:go|jump) to (?:the )?top(?: of the page)?", {"action": "scroll", "dir": "top"}),
    (r"scroll to (?:the )?bottom|(?:go|jump) to (?:the )?bottom(?: of the page)?", {"action": "scroll", "dir": "bottom"}),
    (r"(?:turn )?(?:the )?volume up|turn up the volume|louder", {"action": "volume", "op": "up"}),
    (r"(?:turn )?(?:the )?volume down|turn down the volume|quieter", {"action": "volume", "op": "down"}),
    (r"mute(?: it| the sound| the volume)?", {"action": "volume", "op": "mute"}),
    (r"unmute(?: it| the sound| the volume)?", {"action": "volume", "op": "unmute"}),
    (r"(?:play|pause|resume)(?: (?:the )?(?:music|song|video)| it)?", {"action": "media", "op": "play_pause"}),
    (r"next (?:song|track)|skip(?: (?:this |the )?(?:song|track))?", {"action": "media", "op": "next"}),
    (r"previous (?:song|track)", {"action": "media", "op": "prev"}),
]
FIXED_RE = [(re.compile(rf"^(?:{p})$"), a) for p, a in FIXED]
NEW_TAB = re.compile(r"^(?:(?:open|create|make) )?(?:(?:a|another|one more) )?(?:new )?tab$")
SCROLL = re.compile(r"^scroll (up|down)(?: (a little|a bit|a lot|more))?$")
AMOUNT = {None: "page", "a little": "small", "a bit": "small", "a lot": "large", "more": "page"}

KEYS = {
    "control": "ctrl", "ctrl": "ctrl", "shift": "shift", "alt": "alt",
    "windows": "win", "win": "win", "enter": "enter", "return": "enter",
    "escape": "esc", "esc": "esc", "tab": "tab", "space": "space", "spacebar": "space",
    "backspace": "backspace", "delete": "delete", "up": "up", "down": "down",
    "left": "left", "right": "right", "home": "home", "end": "end",
}
KEY_FILLER = {"the", "key", "plus", "and", "arrow"}


def _keys(words: list[str]) -> str | None:
    """"control shift t" -> "ctrl+shift+t". Any unknown word -> None."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in KEY_FILLER:
            i += 1
        elif w == "f" and nxt.isdigit():
            out.append("f" + nxt)
            i += 2
        elif w == "page" and nxt in ("up", "down"):
            out.append("page" + nxt)
            i += 2
        elif w in KEYS:
            out.append(KEYS[w])
            i += 1
        elif re.fullmatch(r"f\d{1,2}|[a-z0-9]", w):
            out.append(w)
            i += 1
        else:
            return None
    return "+".join(out) if out else None


def _window_target(text: str | None, cat: Catalog, ctx: Ctx) -> str | None:
    """None/pronoun -> the app in context, else "active". A name -> that app."""
    if text is None or re.fullmatch(PRONOUN, text):
        return ctx.last_app or "active"
    hit = resolve(text.split(), cat)
    if hit and hit[0] == "app":
        ctx.last_app = hit[1]
        return hit[1]
    return None


def _strip_submit(toks: list[Tok]) -> tuple[list[Tok], bool]:
    """"hello and press enter" -> ("hello", True)."""
    n = [t.norm for t in toks]
    tails = [lead + [verb, key]
             for lead in (["and", "then"], ["and"], ["then"], [])
             for verb in ("press", "hit") for key in ("enter", "return")]
    tails += [["and", "send", "it"], ["and", "send"], ["and", "submit"]]
    for tail in tails:
        if len(n) > len(tail) and n[-len(tail):] == tail:
            return toks[: -len(tail)], True
    return toks, False


def _payload_clause(toks: list[Tok], cat: Catalog):
    """type/search commands. None: not one. []: one, but with no payload."""
    n = [t.norm for t in toks]
    if n[0] in ("type", "write", "dictate"):
        rest, submit = _strip_submit(toks[1:])
        return [{"action": "type_text", "text": _payload(rest), "submit": submit}] if rest else []
    engine, rest = "google", None
    if n[0] == "search":
        if len(n) > 2 and n[1] in cat.engines and n[2] == "for":     # search youtube for X
            engine, rest = n[1], toks[3:]
        elif len(n) > 1 and n[1] in ("for", "up"):                   # search for X
            rest = toks[2:]
        else:                                                        # search X [up]
            rest = toks[1:]
            if len(rest) > 1 and rest[-1].norm == "up":
                rest = rest[:-1]
    elif n[:2] == ["look", "up"]:
        rest = toks[2:]
    elif n[0] == "google" and len(n) > 1:
        rest = toks[1:]
    if rest is None:
        return None
    if len(rest) > 2 and rest[-2].norm == "on" and rest[-1].norm in cat.engines:   # X on youtube
        engine, rest = rest[-1].norm, rest[:-2]
    return [{"action": "web_search", "engine": engine, "query": _payload(rest)}] if rest else []


def _open(verb: str, what: str, cat: Catalog, ctx: Ctx):
    """open/switch <app | site | domain> [on the other screen | in a new tab]."""
    monitor = None
    m = MON_TAIL.match(what)
    if m:
        what, monitor = m.group("head"), MON_MAP[m.group("mon")]
    else:
        for tail in (TAB_TAIL, BROWSER_TAIL):
            m = tail.match(what)
            if m:
                what = m.group("head")
                break
    hit = resolve(what.split(), cat)
    if not hit:
        return None
    if hit[0] == "app":
        ctx.last_app = hit[1]
        acts = [{"action": "open_app" if verb == "open" else "switch_app", "app": hit[1]}]
        if monitor:
            acts.append({"action": "window", "op": "move_monitor", "target": hit[1], "monitor": monitor})
        return acts
    if monitor:
        return None
    return [{"action": "open_url", "url": cat.sites[hit[1]] if hit[0] == "site" else hit[1]}]


def _close(what: str, cat: Catalog):
    hit = resolve(what.split(), cat)
    if hit and hit[0] == "app":
        return [{"action": "close_app", "app": hit[1]}]
    if hit and hit[0] == "site":
        return [{"action": "tab", "op": "close", "site": hit[1]}]
    return None


def _clause(toks: list[Tok], cat: Catalog, ctx: Ctx, inherit: str | None):
    """Exactly one command -> its actions, or None."""
    if not toks:
        return None
    s = _text(toks)

    # "open chrome and fusion": a bare target reuses the previous verb
    if inherit in ("open", "switch"):
        acts = _open(inherit, s, cat, ctx)
        if acts:
            return acts
    elif inherit == "close":
        acts = _close(s, cat)
        if acts:
            return acts

    acts = _payload_clause(toks, cat)
    if acts is not None:
        return acts or None
    if len(toks) > MAX_COMMAND_WORDS:
        return None

    for rx, act in FIXED_RE:
        if rx.match(s):
            return [dict(act)]
    if NEW_TAB.match(s) and s != "tab":
        return [{"action": "tab", "op": "new"}]
    m = SCROLL.match(s)
    if m:
        return [{"action": "scroll", "dir": m.group(1), "amount": AMOUNT[m.group(2)]}]
    m = re.match(r"^(?:press|hit|tap) (.+)$", s)
    if m:
        keys = _keys(m.group(1).split())
        return [{"action": "press", "keys": keys}] if keys else None

    m = (re.match(r"^(?:maximize|full ?screen)(?: (.+))?$", s)
         or re.match(r"^(?:make|set) (.+?) (?:to )?(?:full ?screen|maximized)$", s))
    if m:
        t = _window_target(m.group(1), cat, ctx)
        return [{"action": "window", "op": "maximize", "target": t}] if t else None
    m = re.match(r"^(minimize|restore)(?: (.+))?$", s)
    if m:
        t = _window_target(m.group(2), cat, ctx)
        return [{"action": "window", "op": m.group(1), "target": t}] if t else None
    m = re.match(rf"^(?:{MOVE_VERB}) (.+)$", s)
    if m:
        mm = MON_TAIL.match(m.group(1))
        t = _window_target(mm.group("head"), cat, ctx) if mm else None
        if not t:
            return None
        return [{"action": "window", "op": "move_monitor", "target": t,
                 "monitor": MON_MAP[mm.group("mon")]}]
    m = re.match(r"^snap(?: (.+?))?(?: to)?(?: the)? (left|right)$", s)
    if m:
        t = _window_target(m.group(1), cat, ctx)
        return [{"action": "window", "op": f"snap_{m.group(2)}", "target": t}] if t else None

    m = re.match(rf"^(?:{CLOSE_VERB}) (.+)$", s)
    if m:
        if re.fullmatch(PRONOUN, m.group(1)):
            return [{"action": "window", "op": "close", "target": ctx.last_app or "active"}]
        return _close(m.group(1), cat)
    m = re.match(rf"^(?:{OPEN_VERB}) (.+)$", s)
    if m:
        return _open("open", m.group(1), cat, ctx)
    m = re.match(rf"^(?:{SWITCH_VERB}) (.+)$", s)
    if m:
        return _open("switch", m.group(1), cat, ctx)
    return None


_VERB = {"open_app": "open", "open_url": "open", "switch_app": "switch", "close_app": "close"}


def _splits(toks: list[Tok]):
    """Every word boundary is a candidate split: yield (head_end, tail_start).

    Commands may be joined by a comma, "and", "then", "and then", a sentence
    break, or nothing, so it does not matter what punctuation the STT emits.
    """
    for i in range(1, len(toks)):
        if toks[i].norm == "and" and i + 1 < len(toks) and toks[i + 1].norm == "then":
            yield i, i + 2
        elif toks[i].norm in ("and", "then"):
            yield i, i + 1
        else:
            yield i, i


def _sequence(toks: list[Tok], cat: Catalog, ctx: Ctx, inherit: str | None):
    """Whole thing as one command first; otherwise the first split where the
    head is one complete command and the rest parses the same way."""
    saved = ctx.last_app
    acts = _clause(toks, cat, ctx, inherit)
    if acts is not None:
        return acts
    if len(toks) > MAX_COMMAND_WORDS:
        return None
    for head_end, tail_start in _splits(toks):
        ctx.last_app = saved
        head = _clause(toks[:head_end], cat, ctx, inherit)
        if head is None:
            continue
        tail = _sequence(toks[tail_start:], cat, ctx, _VERB.get(head[0]["action"]))
        if tail is not None:
            return head + tail
    ctx.last_app = saved
    return None


def parse(utterance: str, catalog: Catalog, last_app: str | None = None) -> list[dict] | None:
    """last_app: the app the previous utterance referred to, so "it" has a referent."""
    toks = _strip_politeness(tokenize(utterance))
    if not toks:
        return None
    return _sequence(toks, catalog, Ctx(last_app), None)
