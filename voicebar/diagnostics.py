"""Pure failure explanations; suggestions never turn into a plan."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from . import parser
from .catalog import Catalog


def _target_words(query: str) -> list[str]:
    words = [token.norm for token in parser.tokenize(query)]
    while words and words[0] in parser.ARTICLES:
        words.pop(0)
    while words and words[0] in parser.NOISE_LEAD:
        words.pop(0)
    while words and words[-1] in parser.NOISE_TRAIL:
        words.pop()
    return words


def nearest_candidates(query: str, catalog: Catalog, limit: int = 5) -> list[dict]:
    """Rank app/site names and aliases using the resolver's matching rules."""
    words = _target_words(query)
    normalized = " ".join(words)
    if not normalized:
        return []
    query_key = parser._squash(normalized)
    candidates: list[dict] = []
    # Keep repeated display names: two installed AppIDs are genuinely ambiguous.
    for kind, name in [("app", name) for name in catalog.apps] + [
        ("site", name) for name in catalog.sites
    ]:
        phrases = [name]
        if kind == "app":
            phrases += [phrase for phrase, target in catalog.aliases.items() if target == name]
        scored = []
        for phrase in phrases:
            if parser._squash(phrase) == query_key:
                score = 100.0
            elif kind == "app" and set(words) <= set(phrase.lower().split()):
                score = float(95 - 2 * (len(phrase.lower().split()) - len(set(words))))
            else:
                score = fuzz.ratio(query_key, parser._squash(phrase))
            scored.append((score, phrase))
        score, phrase = max(scored)
        candidates.append(
            {"kind": kind, "name": name, "score": round(score, 2), "matched_phrase": phrase}
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["name"].casefold()))[:limit]


def _explicit_target(text: str) -> str | None:
    match = re.fullmatch(
        rf"(?:{parser.OPEN_VERB}|{parser.SWITCH_VERB}|{parser.CLOSE_VERB}) (.+)", text
    )
    if match:
        target = match.group(1)
        for suffix in (parser.MON_TAIL, parser.TAB_TAIL, parser.BROWSER_TAIL):
            tail = suffix.match(target)
            if tail:
                return tail.group("head")
        return target
    # These are parser grammar patterns, supplied by the same registry used for parsing/docs.
    for name, group in (
        ("maximize", 1), ("set_maximize", 1), ("minimize_restore", 2), ("snap", 1),
        ("move_monitor", 1),
    ):
        match = re.fullmatch(parser.CLAUSE_PATTERNS[name], text)
        if not match:
            continue
        target = match.group(group)
        if name == "move_monitor" and target:
            tail = parser.MON_TAIL.match(target)
            return tail.group("head") if tail else None
        return target
    return None


def explain_failure(
    utterance: str, catalog: Catalog, app_ids: dict[str, list[str]] | None = None
) -> dict:
    """Explain failed target-bearing clauses without guessing an executable action."""
    tokens = parser._strip_politeness(parser.tokenize(utterance))
    findings = []

    def consider(text: str, inherit: bool = False) -> None:
        target = _explicit_target(text)
        if target is None and inherit:
            target = text
        if not target or re.fullmatch(parser.PRONOUN, target):
            return
        if parser.resolve(target.split(), catalog) is not None:
            return
        candidates = nearest_candidates(target, catalog)
        if app_ids is not None:
            distinct = []
            seen_candidates = set()
            for candidate in candidates:
                key = (candidate["kind"], candidate["name"])
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                if candidate["kind"] == "app":
                    candidate["app_ids"] = app_ids.get(candidate["name"], [])
                distinct.append(candidate)
            candidates = distinct
        if candidates:
            findings.append({"query": target, "candidates": candidates})

    # A failed first target may be followed by an otherwise valid command.
    for head_end, tail_start in parser._splits(tokens):
        tail = " ".join(token.raw for token in tokens[tail_start:])
        if parser.parse(tail, catalog) is not None:
            consider(parser._text(tokens[:head_end]))
    for offset in range(len(tokens)):
        prefix = tokens[:offset]
        while prefix and prefix[-1].norm in {"and", "then"}:
            prefix = prefix[:-1]
        preceding = parser.parse(" ".join(token.raw for token in prefix), catalog) if prefix else []
        if offset and not preceding:
            continue
        suffix = tokens[offset:]
        while suffix and suffix[0].norm in {"and", "then"}:
            suffix = suffix[1:]
        if not suffix:
            continue
        text = parser._text(suffix)
        consider(text, bool(preceding and preceding[0]["action"] in parser._VERB))
    # Prefer the shortest failed clause: longer ones can include a valid earlier chain.
    findings.sort(key=lambda item: (len(item["query"].split()), len(item["query"])))
    unique = []
    seen = set()
    for finding in findings:
        if finding["query"] not in seen:
            unique.append(finding)
            seen.add(finding["query"])
    return {
        "reason": "unknown_or_ambiguous_target" if unique else "unrecognized_command",
        "targets": unique[:3],
    }
