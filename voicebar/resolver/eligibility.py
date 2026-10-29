"""Conservative request grammar before any transcript reaches a selector.

This recognizes request *form*, not an executable action. A bounded polite
prefix must lead to a known command verb. Statements, factual/advice questions,
reported or hypothetical instructions, and negated instructions fail closed.
The candidate builder and selected-plan validation still decide which actions
are possible. Unknown verbs and indirect requests intentionally remain unsure.

Typing and searching swallow their remainder, as in tier 1. That remainder is
opaque: questions, quotation marks, and negation there are literal payload, not
new instructions. A negated clause before such a payload rejects the entire
utterance rather than permitting an earlier positive action.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[\w]+(?:['\u2019][\w]+)?", re.UNICODE)
_FILLERS = {
    "please",
    "just",
    "hey",
    "ok",
    "okay",
    "well",
    "uh",
    "um",
    "erm",
    "so",
    "like",
    "actually",
}
_COMMAND_HEADS = frozenset(
    [
        "advance",
        "back",
        "be",
        "blow",
        "boot",
        "bring",
        "close",
        "continue",
        "dictate",
        "dismiss",
        "drop",
        "enlarge",
        "enter",
        "expand",
        "fill",
        "find",
        "fire",
        "flip",
        "focus",
        "get",
        "give",
        "go",
        "hide",
        "hit",
        "hunt",
        "increase",
        "jot",
        "jump",
        "launch",
        "let",
        "load",
        "look",
        "lower",
        "make",
        "maximize",
        "minimize",
        "move",
        "mute",
        "nudge",
        "open",
        "pause",
        "play",
        "press",
        "pull",
        "put",
        "raise",
        "reduce",
        "relocate",
        "reload",
        "reopen",
        "restore",
        "resume",
        "return",
        "reveal",
        "scroll",
        "search",
        "send",
        "shift",
        "show",
        "shrink",
        "shut",
        "silence",
        "skip",
        "sleep",
        "snap",
        "spin",
        "spread",
        "start",
        "step",
        "stop",
        "switch",
        "take",
        "tap",
        "throw",
        "toggle",
        "tuck",
        "turn",
        "type",
        "unmute",
        "use",
        "wake",
        "write",
    ]
)
_REQUEST_PREFIXES = (
    ("i", "would", "like", "you", "to"),
    ("i'd", "like", "you", "to"),
    ("i", "want", "you", "to"),
    ("i", "need", "you", "to"),
    ("could", "you"),
    ("would", "you"),
    ("can", "you"),
    ("will", "you"),
    ("do", "me", "a", "favor", "and"),
    ("do", "me", "a", "favour", "and"),
    ("go", "ahead", "and"),
)
_PAYLOAD_HEADS = (
    ("put", "the", "words", "in"),
    ("put", "these", "words", "in"),
    ("jot", "this", "down"),
    ("jot", "that", "down"),
    ("look", "online", "for"),
    ("search", "the", "web", "for"),
    ("jot", "down"),
    ("fill", "in"),
    ("put", "in"),
    ("look", "for"),
    ("look", "up"),
    ("hunt", "for"),
    ("type",),
    ("write",),
    ("dictate",),
    ("enter",),
    ("search",),
    ("find",),
)
_NEGATION = frozenset(
    [
        "no",
        "not",
        "never",
        "nothing",
        "none",
        "neither",
        "nor",
        "without",
        "except",
        "unless",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "can't",
        "cannot",
        "shouldn't",
        "couldn't",
        "wouldn't",
        "aren't",
        "isn't",
        "wasn't",
        "weren't",
        "haven't",
        "hasn't",
        "hadn't",
    ]
)
_UNCERTAIN_OR_DEFERRED = frozenset(
    [
        "if",
        "whether",
        "maybe",
        "perhaps",
        "probably",
        "possibly",
        "someday",
        "sometime",
        "later",
        "tomorrow",
        "eventually",
        "pretend",
        "suppose",
        "supposing",
        "imagine",
        "hypothetically",
        "could",
        "would",
        "should",
        "might",
    ]
)
_CONVERSATION = frozenset(
    [
        "wonder",
        "wondering",
        "prefer",
        "wish",
        "hope",
        "think",
        "consider",
        "guess",
        "explain",
        "explaining",
        "discuss",
        "describe",
        "joke",
        "joking",
        "kidding",
        "thinking",
        "considering",
        "hoping",
        "preferring",
        "cancel",
        "abort",
        "forget",
        "disregard",
    ]
)
_REPORTED = re.compile(
    r"\b(?:i|we|he|she|they|someone|somebody|you)\s+"
    r"(?:said|asked|told|meant|mentioned|suggested|wrote|heard)\b"
    r"|\b(?:for|as\s+an?)\s+example\b"
    r"|\b(?:i\s+am|i'm)\s+(?:quoting|saying|talking)\b"
    r"|\b(?:rather\s+than|instead\s+of|never\s+mind)\b"
    r"|\b(?:that\s+is|that's)\s+enough\b"
    r"|\b(?:changed\s+my\s+mind|hold\s+on)\b"
    r"|\b(?:show|tell|teach|explain|describe)\s+(?:(?:me|us)\s+)?"
    r"(?:how|why|what|whether|which|when)\b"
)
_COPULAS = {"is", "are", "was", "were"}
_FINITE_PREDICATES = {
    "save",
    "help",
    "cause",
    "make",
    "take",
    "keep",
    "mean",
    "have",
    "need",
    "seem",
    "look",
    "feel",
    "work",
}
_CAUSATIVE_OR_LOCATION_HEADS = {"make", "let", "get", "put", "bring", "take", "move"}


def _starts(words: list[str], offset: int, phrase: tuple[str, ...]) -> bool:
    return tuple(words[offset : offset + len(phrase)]) == phrase


def _command_start(words: list[str]) -> int:
    """Allow one request wrapper and at most six spoken filler words."""
    offset = 0
    filler_count = 0
    while offset < len(words) and words[offset] in _FILLERS:
        offset += 1
        filler_count += 1
    for prefix in _REQUEST_PREFIXES:
        if _starts(words, offset, prefix):
            offset += len(prefix)
            break
    while offset < len(words) and words[offset] in _FILLERS:
        offset += 1
        filler_count += 1
    return offset if filler_count <= 6 else len(words)


def _clause_start(
    text: str, tokens: list, words: list[str], index: int, start: int
) -> bool:
    if index == start or words[index - 1] in {"and", "then"}:
        return True
    between = text[tokens[index - 1].end() : tokens[index].start()]
    return any(char in between for char in ",;.!?\n")


def _payload_command(text: str, tokens: list, words: list[str], start: int):
    """Locate a payload command at the start or an explicit clause boundary."""
    for boundary in range(start, len(words)):
        if not _clause_start(text, tokens, words, boundary, start):
            continue
        index = boundary + _command_start(words[boundary:])
        for phrase in _PAYLOAD_HEADS:
            if _starts(words, index, phrase) and index + len(phrase) < len(words):
                family = "web_search" if phrase[0] in {"look", "hunt", "search", "find"} else "type_text"
                return index, len(phrase), family
    return None


def payload_command(text: str):
    """Raw offset and family of the first clause-level payload command.

    Candidate construction shares this boundary with the request guard, so a
    verb inside an app name, explanation, or literal payload cannot start a
    second payload. The returned offset is only used for slicing raw text.
    """
    tokens = list(_TOKEN.finditer(text))
    words = [token.group().casefold().replace("\u2019", "'") for token in tokens]
    start = _command_start(words)
    if start >= len(words) or words[start] not in _COMMAND_HEADS:
        return None
    command = _payload_command(text, tokens, words, start)
    if command is None:
        return None
    index, _, family = command
    return tokens[index].start(), family


def minimum_actions(text: str) -> int:
    """Count explicit request clauses, excluding the opaque payload remainder."""
    tokens = list(_TOKEN.finditer(text))
    words = [token.group().casefold().replace("\u2019", "'") for token in tokens]
    start = _command_start(words)
    if start >= len(words):
        return 1
    payload = _payload_command(text, tokens, words, start)
    limit = payload[0] if payload else len(words) - 1
    heads = set()
    for boundary in range(start, limit + 1):
        if words[boundary] in {"and", "then"}:
            continue
        if _clause_start(text, tokens, words, boundary, start):
            head = boundary + _command_start(words[boundary:])
            if head <= limit and words[head] in _COMMAND_HEADS:
                heads.add(head)
    return max(1, len(heads))


def _declarative_clause(words: list[str], index: int, end: int) -> bool:
    """Reject a command word used as a noun modifier or sentence subject."""
    if words[index] not in _COMMAND_HEADS:
        return False
    remainder = words[index + 1 : min(end, index + 5)]
    for boundary, word in enumerate(remainder):
        if word in {"and", "then"}:
            remainder = remainder[:boundary]
            break
    if remainder and remainder[0] in _COPULAS:
        return True
    if words[index] in _CAUSATIVE_OR_LOCATION_HEADS:
        return False
    if any(word in _COPULAS for word in remainder):
        return True
    return bool(
        len(remainder) >= 2
        and remainder[0].endswith("s")
        and remainder[1] in _FINITE_PREDICATES
    )


def _pointer_description(words: list[str], index: int) -> bool:
    """'Display without the pointer' describes a destination, not an opt-out."""
    if index == 0 or words[index - 1] not in {"screen", "monitor", "display"}:
        return False
    after = index + 1
    if after < len(words) and words[after] in {"a", "the", "my"}:
        after += 1
    return after < len(words) and words[after] in {"mouse", "pointer", "cursor"}


def eligible_request(text: str) -> bool:
    """Whether wake-stripped speech is structurally an immediate request.

    A true result only permits candidate construction; it never authorizes an
    action. This deliberately rejects unrecognized phrasing instead of asking a
    language model to infer that conversation was intended as a command.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        return False
    tokens = list(_TOKEN.finditer(text))
    words = [token.group().casefold().replace("\u2019", "'") for token in tokens]
    if not words or len(words) > 40:
        return False
    start = _command_start(words)
    if start >= len(words) or words[start] not in _COMMAND_HEADS:
        return False
    # Quotes around a whole requested instruction are reported text. Quotes
    # following a typing/search verb belong to its payload and are permitted.
    before_head = text[: tokens[start].start()]
    if any(char in before_head for char in "\"'\u2018\u2019\u201c\u201d"):
        # Apostrophes within a recognized polite contraction are not quotes.
        wrapper = before_head.casefold().replace("i'd", "id").replace("i\u2019d", "id")
        if any(char in wrapper for char in "\"'\u2018\u2019\u201c\u201d"):
            return False
    payload_command_info = _payload_command(text, tokens, words, start)
    payload = payload_command_info[0] if payload_command_info else len(words)
    # Every explicit command clause needs a recognized request head. A model
    # cannot silently drop an unsupported second request and execute the first.
    for boundary in range(start, min(payload + 1, len(words))):
        if words[boundary] in {"and", "then"}:
            continue
        if _clause_start(text, tokens, words, boundary, start):
            head = boundary + _command_start(words[boundary:])
            if head > payload or head >= len(words) or words[head] not in _COMMAND_HEADS:
                return False
    if any(
        _clause_start(text, tokens, words, index, start)
        and _declarative_clause(words, index, payload)
        for index in range(start, payload)
    ):
        return False
    command_words = words[start:payload]
    if any(
        word in _NEGATION | _UNCERTAIN_OR_DEFERRED | _CONVERSATION
        and not (word == "without" and _pointer_description(command_words, index))
        for index, word in enumerate(command_words)
    ):
        return False
    command_text = " ".join(command_words)
    return not _REPORTED.search(command_text)
