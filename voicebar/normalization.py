"""Shared, pure phrase normalization for transcripts and catalog identities."""
from __future__ import annotations

import re
from functools import lru_cache

EDGE = re.compile(r"^[^\w']+|[^\w']+$")


def normalized_words(text: str) -> tuple[str, ...]:
    return tuple(norm for raw in text.split() if (norm := EDGE.sub("", raw.lower())))


@lru_cache(maxsize=4096)
def phrase_key(text: str) -> str:
    """Normalize both sides of exact matching; bounded cache holds only text keys."""
    return "".join(normalized_words(text))
