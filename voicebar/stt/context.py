"""Fixture-independent ASR context from live spoken grammar and app catalog.

Qwen supports system-context vocabulary, not reliable instruction following.
Only the explicit grammar constants below are inspected. Contract examples,
expected plans, action dictionaries and recording payloads never enter context.
"""

import re

from .. import parser as grammar
from .base import keyterms

CONTEXT_PROFILE = "desktop-grammar-catalog-v1"
DOMAIN_CONTEXT = (
    "Windows desktop voice commands for applications, windows, browser tabs, "
    "typing, web search, and media playback."
)

_PATTERNS = (
    "PRONOUN", "MON_WORD", "MON_TAIL", "TAB_TAIL", "BROWSER_TAIL",
    "OPEN_VERB", "SWITCH_VERB", "CLOSE_VERB", "MOVE_VERB", "NEW_TAB", "SCROLL", "KEY_ATOM",
)
_STRINGS = (
    "SEARCH_VERB", "ENGINE_QUERY_JOINER", "DEFAULT_ENGINE", "SEARCH_TRAILING",
    "ENGINE_SUFFIX", "KEY_NUMBER_PREFIX", "KEY_PAGE_PREFIX",
)
_COLLECTIONS = (
    "TYPE_VERBS", "SEARCH_PREPOSITIONS", "LOOKUP_PREFIX", "KEY_PAGE_DIRECTIONS",
    "KEY_FILLER", "ARTICLES", "NOISE_LEAD", "NOISE_TRAIL",
)
_PHRASES = ("LEAD", "TRAIL", "SUBMIT_TAILS", "CHAIN_JOINERS")
_MAPPING_KEYS = ("KEYS", "MON_MAP", "AMOUNT")


def literal_words(pattern: str) -> tuple[str, ...]:
    """Collect alphabetic literals, excluding regex metadata and character sets.

    This is vocabulary extraction, not a regex expander. A class such as [a-z]
    contributes no guessed words; named capture/backreference labels, comments,
    flags and escape codes likewise contribute none. Group bodies are retained.
    """
    cleaned = []
    index, length = 0, len(pattern)
    while index < length:
        if pattern[index] == "\\":
            if index + 1 == length:
                raise ValueError("Grammar context contains a trailing regex escape.")
            code = pattern[index + 1]
            if code in "xuU":
                index += 2 + {"x": 2, "u": 4, "U": 8}[code]
            elif code == "N" and pattern[index + 2:index + 3] == "{":
                end = pattern.find("}", index + 3)
                if end < 0:
                    raise ValueError("Grammar context contains an unfinished named escape.")
                index = end + 1
            else:
                index += 2
                if code.isdigit():
                    while index < length and pattern[index].isdigit():
                        index += 1
            cleaned.append(" ")
        elif pattern[index] == "[":
            index += 1
            if pattern[index:index + 1] == "^":
                index += 1
            if pattern[index:index + 1] == "]":
                index += 1  # an initial ] is a literal member, not the closer
            while index < length and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            if index >= length:
                raise ValueError("Grammar context contains an unfinished character class.")
            index += 1
            cleaned.append(" ")
        elif pattern.startswith("(?P<", index):
            end = pattern.find(">", index + 4)
            if end < 0:
                raise ValueError("Grammar context contains an unfinished capture name.")
            index = end + 1
            cleaned.append(" ")
        elif pattern.startswith(("(?P=", "(?#"), index):
            end = pattern.find(")", index + 3)
            if end < 0:
                raise ValueError("Grammar context contains an unfinished regex metadata group.")
            index = end + 1
            cleaned.append(" ")
        elif pattern.startswith("(?", index):
            flag = re.match(r"\(\?[aiLmsux-]+(?::|\))", pattern[index:])
            if flag:
                index += len(flag.group())
            else:
                # Noncapturing, lookaround and atomic group prefixes carry no
                # words. Consume their punctuation; keep the subsequent body.
                index += 2
                while index < length and pattern[index] in ":=!<>":
                    index += 1
            cleaned.append(" ")
        else:
            cleaned.append(pattern[index])
            index += 1
    return tuple(re.findall(r"[a-z]+", "".join(cleaned).lower()))


def grammar_terms() -> tuple[str, ...]:
    """Read only spoken strings/patterns from the explicit live-grammar list."""
    expressions = [getattr(grammar, name) for name in _PATTERNS + _STRINGS]
    expressions.extend(grammar.CLAUSE_PATTERNS.values())
    expressions.extend(pattern for pattern, _action in grammar.FIXED)
    for name in _COLLECTIONS:
        expressions.extend(getattr(grammar, name))
    for name in _PHRASES:
        expressions.extend(word for phrase in getattr(grammar, name) for word in phrase)
    for name in _MAPPING_KEYS:
        expressions.extend(key for key in getattr(grammar, name) if key is not None)
    words = set()
    for expression in expressions:
        words.update(literal_words(expression.pattern if isinstance(expression, re.Pattern) else expression))
    return tuple(sorted(words))


def build_context(catalog) -> str:
    """One fixed catalog/grammar context, with no utterance or expected answer."""
    values = [*grammar_terms(), *keyterms(catalog), *catalog.engines]
    # Stable ordering also makes aliases/catalog insertion order irrelevant.
    vocabulary = {}
    for value in sorted((text.strip() for text in values if text.strip()), key=lambda text: (text.casefold(), text)):
        vocabulary.setdefault(value.casefold(), value)
    return DOMAIN_CONTEXT + "\nVocabulary: " + ", ".join(vocabulary.values()) + "."
