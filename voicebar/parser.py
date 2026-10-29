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
from .normalization import EDGE, phrase_key

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
    action_app: str | None = None  # foreground, or opened/switched earlier in this utterance


class _AmbiguousTarget(Exception):
    """A known exact-name collision must not be reinterpreted as split commands."""


_EDGE = EDGE


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
    ["hey"], ["ok"], ["okay"], ["please"], ["um"], ["uh"], ["ah"], ["now"], ["just"],
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
    return phrase_key(s)


def _exact(q: str, cat: Catalog):
    """Alias, app name, then site key; normalize both catalog and transcript text."""
    sq = _squash(q)
    for k, v in cat.aliases.items():
        if _squash(k) == sq:
            if sum(_squash(a) == _squash(v) for a in cat.apps) > 1:
                return ("ambiguous", q)
            return ("app", v)
    apps = [a for a in cat.apps if _squash(a) == sq]
    if len(apps) > 1:
        return ("ambiguous", q)
    if apps:
        return ("app", apps[0])
    for k in cat.sites:
        if _squash(k) == sq:
            return ("site", k)
    return None


def resolve(words: list[str], cat: Catalog, *, _strict: bool = False, _exact_only: bool = False):
    """-> ("app", name) | ("site", key) | ("url", url) | None if unknown or ambiguous."""
    while words and words[0] in ARTICLES:
        words = words[1:]
    if not words:
        return None
    q = " ".join(words)
    hit = _exact(q, cat)
    if hit:
        if hit[0] == "ambiguous":
            if _strict:
                raise _AmbiguousTarget(q)
            return None
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
            if hit[0] == "ambiguous":
                if _strict:
                    raise _AmbiguousTarget(q)
                return None
            return hit

    if _exact_only:
        return None
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

# Shared with command_docs: these are the patterns the parser actually matches.
CLAUSE_PATTERNS = {
    "press": re.compile(r"^(?:press|hit|tap) (.+)$"),
    "maximize": re.compile(r"^(?:maximize|full ?screen)(?: (.+))?$"),
    "set_maximize": re.compile(r"^(?:make|set) (.+?) (?:to )?(?:full ?screen|maximized)$"),
    "minimize_restore": re.compile(r"^(minimize|restore)(?: (.+))?$"),
    "move_monitor": re.compile(rf"^(?:{MOVE_VERB}) (.+)$"),
    "snap": re.compile(r"^snap(?: (.+?))?(?: to)?(?: the)? (left|right)$"),
    "close": re.compile(rf"^(?:{CLOSE_VERB}) (.+)$"),
    "open": re.compile(rf"^(?:{OPEN_VERB}) (.+)$"),
    "switch": re.compile(rf"^(?:{SWITCH_VERB}) (.+)$"),
}

TYPE_VERBS = ("type", "write", "dictate")
SEARCH_VERB = "search"
SEARCH_PREPOSITIONS = ("for", "up")
ENGINE_QUERY_JOINER = "for"
LOOKUP_PREFIX = ("look", "up")
DEFAULT_ENGINE = "google"
SEARCH_TRAILING = "up"
ENGINE_SUFFIX = "on"
SUBMIT_TAILS = [lead + (verb, key)
                for lead in (("and", "then"), ("and",), ("then",), ())
                for verb in ("press", "hit") for key in ("enter", "return")]
SUBMIT_TAILS += [("and", "send", "it"), ("and", "send"), ("and", "submit")]
CHAIN_JOINERS = (("and", "then"), ("and",), ("then",))

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
KEY_ATOM = re.compile(r"f\d{1,2}|[a-z0-9]")
KEY_NUMBER_PREFIX = "f"
KEY_PAGE_PREFIX = "page"
KEY_PAGE_DIRECTIONS = ("up", "down")


def _keys(words: list[str]) -> str | None:
    """"control shift t" -> "ctrl+shift+t". Any unknown word -> None."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in KEY_FILLER:
            i += 1
        elif w == KEY_NUMBER_PREFIX and nxt.isdigit():
            out.append(KEY_NUMBER_PREFIX + nxt)
            i += 2
        elif w == KEY_PAGE_PREFIX and nxt in KEY_PAGE_DIRECTIONS:
            out.append(KEY_PAGE_PREFIX + nxt)
            i += 2
        elif w in KEYS:
            out.append(KEYS[w])
            i += 1
        elif KEY_ATOM.fullmatch(w):
            out.append(w)
            i += 1
        else:
            return None
    return "+".join(out) if out else None


def _window_target(text: str | None, cat: Catalog, ctx: Ctx) -> str | None:
    """None/pronoun -> the app in context, else "active". A name -> that app."""
    if text is None or re.fullmatch(PRONOUN, text):
        return ctx.last_app or "active"
    hit = resolve(text.split(), cat, _strict=True)
    if hit and hit[0] == "app":
        ctx.last_app = hit[1]
        return hit[1]
    return None


def _strip_submit(toks: list[Tok]) -> tuple[list[Tok], bool]:
    """"hello and press enter" -> ("hello", True)."""
    n = tuple(t.norm for t in toks)
    for tail in SUBMIT_TAILS:
        if len(n) > len(tail) and n[-len(tail):] == tail:
            return toks[: -len(tail)], True
    return toks, False


def _payload_clause(toks: list[Tok], cat: Catalog):
    """type/search commands. None: not one. []: one, but with no payload."""
    n = [t.norm for t in toks]
    if n[0] in TYPE_VERBS:
        rest, submit = _strip_submit(toks[1:])
        return [{"action": "type_text", "text": _payload(rest), "submit": submit}] if rest else []
    engine, rest = DEFAULT_ENGINE, None
    if n[0] == SEARCH_VERB:
        if len(n) > 2 and n[1] in cat.engines and n[2] == ENGINE_QUERY_JOINER:     # search youtube for X
            engine, rest = n[1], toks[3:]
        elif len(n) > 1 and n[1] in SEARCH_PREPOSITIONS:                   # search for X
            rest = toks[2:]
        else:                                                        # search X [up]
            rest = toks[1:]
            if len(rest) > 1 and rest[-1].norm == SEARCH_TRAILING:
                rest = rest[:-1]
    elif tuple(n[:len(LOOKUP_PREFIX)]) == LOOKUP_PREFIX:
        rest = toks[len(LOOKUP_PREFIX):]
    elif n[0] == DEFAULT_ENGINE and len(n) > 1:
        rest = toks[1:]
    if rest is None:
        return None
    if len(rest) > 2 and rest[-2].norm == ENGINE_SUFFIX and rest[-1].norm in cat.engines:   # X on youtube
        engine, rest = rest[-1].norm, rest[:-2]
    return [{"action": "web_search", "engine": engine, "query": _payload(rest)}] if rest else []


def _open(verb: str, what: str, cat: Catalog, ctx: Ctx, *, exact_target: bool = False):
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
    hit = resolve(what.split(), cat, _strict=True, _exact_only=exact_target)
    if not hit:
        return None
    if hit[0] == "app":
        ctx.last_app = hit[1]
        ctx.action_app = hit[1]
        acts = [{"action": "open_app" if verb == "open" else "switch_app", "app": hit[1]}]
        if monitor:
            acts.append({"action": "window", "op": "move_monitor", "target": hit[1], "monitor": monitor})
        return acts
    if monitor:
        return None
    return [{"action": "open_url", "url": cat.sites[hit[1]] if hit[0] == "site" else hit[1]}]


def _close(what: str, cat: Catalog, *, exact_target: bool = False):
    hit = resolve(what.split(), cat, _strict=True, _exact_only=exact_target)
    if hit and hit[0] == "app":
        return [{"action": "close_app", "app": hit[1]}]
    if hit and hit[0] == "site":
        return [{"action": "tab", "op": "close", "site": hit[1]}]
    return None


def _app_action(toks: list[Tok], cat: Catalog, ctx: Ctx):
    """Exact, app-scoped phrases. Payloads here come only from transcript slices."""
    words = [tok.norm for tok in toks]
    if ctx.action_app == "Calculator" and _text(toks) in {"clear", "clear calculator", "clear the calculator"}:
        return [{"action": "app_action", "app": "Calculator", "name": "clear"}]
    # Calculator is an explicit application capability. Normal typing in all
    # other applications remains a verbatim transcript payload.
    if ctx.action_app == "Calculator" and words[0] in {"type", "write", "add", "calculate", "compute", "do"}:
        from .calculator import keypad_sequence
        expression = _payload(toks[1:])
        if keypad_sequence(expression) is not None:
            return [{"action": "app_action", "app": "Calculator", "name": "calculate:" + expression}]
        if words[0] not in TYPE_VERBS:
            return None
    if words[0] == "click":
        offset = 2 if len(words) > 1 and words[1] == "the" else 1
        roles = {"button": "button", "link": "hyperlink", "tab": "tabitem",
                 "menuitem": "menuitem", "checkbox": "checkbox", "field": "edit"}
        if len(words) > offset + 1 and words[offset] in roles:
            label = _payload(toks[offset + 1:])
            return [{"action": "app_action", "app": "active",
                     "name": f"click_role:{roles[words[offset]]}:{label}"}]
        name = _payload(toks[1:])
        return [{"action": "app_action", "app": "active", "name": "click:" + name}] if name else None

    spoken_text = _text(toks)
    explicit_tab = re.fullmatch(r"(open|close) (?:(?:a|the|new) )*(.+) tab", spoken_text)
    if explicit_tab:
        hit = resolve(explicit_tab.group(2).split(), cat, _strict=True)
        whole = resolve(words[1:], cat, _strict=True, _exact_only=True)
        if hit and hit[0] == "site" and whole is None:
            if explicit_tab.group(1) == "open":
                return [{"action": "open_url", "url": cat.sites[hit[1]]}]
            return [{"action": "tab", "op": "close", "site": hit[1]}]
        if hit and hit[0] == "app" and hit[1] in cat.browser_apps:
            preceding = ([] if ctx.action_app == hit[1] else
                         [{"action": "switch_app", "app": hit[1]}])
            ctx.action_app = ctx.last_app = hit[1]
            return preceding + [{"action": "tab", "op": "new" if explicit_tab.group(1) == "open" else "close"}]
    if ctx.action_app in cat.browser_apps:
        if re.fullmatch(r"(?:(?:open|create|make) )?(?:a )?new (?:browser )?window", spoken_text):
            return [{"action": "app_action", "app": "active", "name": "browser_window:new"}]
        tab_prefix = re.match(r"^switch to (?:the )?tab (?:named )?", spoken_text)
        if tab_prefix and " in " not in spoken_text:
            title = _payload(toks[len(tab_prefix.group().split()):])
            if title:
                return [{"action": "app_action", "app": "active", "name": "switch_tab:" + title}]

    # An explicit suffix overrides the foreground app. Try each 'in' boundary
    # because the phrase itself can contain that word.
    scopes = []
    for index in range(1, len(toks) - 1):
        if words[index] != "in":
            continue
        target = resolve(words[index + 1:], cat, _strict=True)
        if target and target[0] == "app":
            scopes.append((target[1], toks[:index]))
    if not scopes:
        scopes.append((ctx.action_app, toks))
    matches = []
    for app, phrase in scopes:
        if app is None or app not in cat.apps:
            continue
        spoken = tuple(tok.norm for tok in phrase)
        for name, definition in cat.app_actions.get(app, {}).items():
            if any(spoken == tuple(tok.norm for tok in tokenize(say))
                   for say in definition["say"]):
                matches.append({"action": "app_action", "app": app, "name": name})

        # Chrome titles are variable, so code cuts the exact title from speech;
        # a definition or model never interpolates transcript text into a step.
        if phrase_key(app) in {"chrome", "googlechrome"}:
            prefix = re.match(r"^switch to (?:the )?tab (?:named )?", _text(phrase))
            if prefix:
                count = len(prefix.group().split())
                title = _payload(phrase[count:])
                if title:
                    matches.append({"action": "app_action", "app": app,
                                    "name": "switch_tab:" + title})
    if len(matches) > 1:
        raise _AmbiguousTarget(_text(toks))
    if matches:
        ctx.action_app = matches[0]["app"]
        ctx.last_app = matches[0]["app"]
        return matches
    return None


def _partial_app_phrase(toks: list[Tok], cat: Catalog) -> bool:
    """Do not split an unfinished app phrase into unrelated generic commands.

    For example, 'start voice' is a prefix of 'start voice mode'; resolving
    'voice' to Voice access would turn a recognition error into a different
    action. Reserve prefixes in every foreground, without fuzzy matching.
    Explicit complete app names such as 'start Voice access' still work.
    """
    spoken = tuple(tok.norm for tok in toks)
    if len(spoken) < 2:
        return False
    return any(
        len(spoken) < len(phrase) and phrase[:len(spoken)] == spoken
        for actions in cat.app_actions.values()
        for definition in actions.values()
        for say in definition["say"]
        for phrase in [tuple(tok.norm for tok in tokenize(say))]
    )


def contains_app_phrase_prefix(text: str, cat: Catalog) -> bool:
    """Reserve app action requests from the optional generic model fallback.

    If an utterance with one of these two-word prefixes did not parse fully,
    a selector cannot replace the unsupported app action with an app launch.
    Successful literal typing/searching never reaches this fallback check.
    """
    prefixes = {
        phrase[:2]
        for actions in cat.app_actions.values()
        for definition in actions.values()
        for say in definition["say"]
        for phrase in [tuple(tok.norm for tok in tokenize(say))]
        if len(phrase) >= 2
    }
    words = [tok.norm for tok in tokenize(text)]
    return any(tuple(words[index:index + 2]) in prefixes for index in range(len(words) - 1))


def _clause(toks: list[Tok], cat: Catalog, ctx: Ctx, inherit: str | None, *, exact_inherit: bool = False):
    """Exactly one command -> its actions, or None."""
    if not toks:
        return None
    s = _text(toks)

    # A known app-scoped phrase may begin with a payload verb (for example
    # Chrome's "search tabs"). Only exact configured/dynamic app grammar gets
    # precedence; all remaining type/search commands still swallow the tail.
    if len(toks) <= MAX_COMMAND_WORDS:
        acts = _app_action(toks, cat, ctx)
        if acts is not None:
            return acts
        if _partial_app_phrase(toks, cat) and _payload_clause(toks, cat) is None:
            return None

    # "open chrome and fusion": a bare target reuses the previous verb.
    # Exact app actions and their unfinished prefixes have priority.
    if inherit in ("open", "switch"):
        acts = _open(inherit, s, cat, ctx, exact_target=exact_inherit)
        if acts:
            return acts
    elif inherit == "close":
        acts = _close(s, cat, exact_target=exact_inherit)
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
    m = CLAUSE_PATTERNS["press"].match(s)
    if m:
        keys = _keys(m.group(1).split())
        return [{"action": "press", "keys": keys}] if keys else None

    m = (CLAUSE_PATTERNS["maximize"].match(s)
         or CLAUSE_PATTERNS["set_maximize"].match(s))
    if m:
        t = _window_target(m.group(1), cat, ctx)
        return [{"action": "window", "op": "maximize", "target": t}] if t else None
    m = CLAUSE_PATTERNS["minimize_restore"].match(s)
    if m:
        t = _window_target(m.group(2), cat, ctx)
        return [{"action": "window", "op": m.group(1), "target": t}] if t else None
    m = CLAUSE_PATTERNS["move_monitor"].match(s)
    if m:
        mm = MON_TAIL.match(m.group(1))
        t = _window_target(mm.group("head"), cat, ctx) if mm else None
        if not t:
            return None
        return [{"action": "window", "op": "move_monitor", "target": t,
                 "monitor": MON_MAP[mm.group("mon")]}]
    m = CLAUSE_PATTERNS["snap"].match(s)
    if m:
        t = _window_target(m.group(1), cat, ctx)
        return [{"action": "window", "op": f"snap_{m.group(2)}", "target": t}] if t else None

    m = CLAUSE_PATTERNS["close"].match(s)
    if m:
        if re.fullmatch(PRONOUN, m.group(1)):
            return [{"action": "window", "op": "close", "target": ctx.last_app or "active"}]
        return _close(m.group(1), cat)
    m = CLAUSE_PATTERNS["open"].match(s)
    if m:
        return _open("open", m.group(1), cat, ctx)
    m = CLAUSE_PATTERNS["switch"].match(s)
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
        for joiner in CHAIN_JOINERS:
            if tuple(t.norm for t in toks[i:i + len(joiner)]) == joiner:
                yield i, i + len(joiner)
                break
        else:
            yield i, i


def _sequence(toks: list[Tok], cat: Catalog, ctx: Ctx, inherit: str | None, *, exact_inherit: bool = False):
    """Whole thing as one command first; otherwise the first split where the
    head is one complete command and the rest parses the same way."""
    saved = ctx.last_app, ctx.action_app
    acts = _clause(toks, cat, ctx, inherit, exact_inherit=exact_inherit)
    if acts is not None:
        return acts
    if len(toks) > MAX_COMMAND_WORDS:
        return None
    for head_end, tail_start in _splits(toks):
        ctx.last_app, ctx.action_app = saved
        head = _clause(toks[:head_end], cat, ctx, inherit, exact_inherit=exact_inherit)
        if head is None:
            continue
        # No verb and no separator is weak evidence of a second command. Only
        # exact catalog names/aliases may inherit the verb at that boundary;
        # an incidental suffix such as "tab" cannot subset-match an installed
        # application. Explicit commands and separated chains keep normal
        # target resolution, including their existing ambiguity checks.
        separated = tail_start != head_end or bool(re.search(r"[,.;:!?]$", toks[head_end - 1].raw))
        tail = _sequence(toks[tail_start:], cat, ctx, _VERB.get(head[0]["action"]),
                         exact_inherit=not separated)
        if tail is not None:
            return head + tail
    ctx.last_app, ctx.action_app = saved
    return None


def parse(utterance: str, catalog: Catalog, last_app: str | None = None, *,
          active_app: str | None = None) -> list[dict] | None:
    """last_app supplies 'it'; active_app scopes bare in-app phrases without I/O."""
    if active_app is None:
        active_app = catalog.active_app
    key = " ".join(t.norm for t in tokenize(utterance))
    if key in catalog.noncommands:
        return None
    learned = catalog.command_aliases.get(key)
    if learned is not None:
        # App actions must pass the foreground/explicit-target grammar below.
        # A learned plan cannot invent an action id or a control name.
        if any(a.get("action") == "app_action" for a in learned):
            return None
        if any(a[field] != "active" and a[field] not in catalog.apps
               for a in learned for field in ("app", "target") if field in a):
            return None
        # Learned payloads must still occur verbatim in this transcript.
        if any(a[field] not in utterance for a in learned
               for field in ("text", "query") if field in a):
            return None
        return [dict(a) for a in learned]
    toks = _strip_politeness(tokenize(utterance))
    if not toks:
        return None
    try:
        return _sequence(toks, catalog, Ctx(last_app, active_app), None)
    except _AmbiguousTarget:
        return None
