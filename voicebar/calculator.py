"""Pure, bounded speech arithmetic to a closed Calculator keypad sequence.

Only one binary operation is accepted: its meaning does not depend on the
Calculator's Standard versus Scientific precedence rules. No expression is
evaluated, and no arbitrary text or shortcut can enter the returned sequence.
"""
from __future__ import annotations

import re

_SMALL = dict(zip([
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
], range(20)))
_TENS = dict(zip(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"],
                 range(20, 100, 10)))
_OPERATORS = {
    "+": "+", "plus": "+", "added to": "+",
    "-": "-", "minus": "-", "subtract": "-",
    "*": "*", "times": "*", "multiplied by": "*",
    "/": "/", "divided by": "/", "over": "/",
}
_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]+)?|\.[0-9]+|[a-z]+|[+*/.-]")


def _under_hundred(words):
    if len(words) == 1:
        return _SMALL.get(words[0], _TENS.get(words[0]))
    if len(words) == 2 and words[0] in _TENS and words[1] in _SMALL:
        units = _SMALL[words[1]]
        return _TENS[words[0]] + units if 1 <= units <= 9 else None
    return None


def _integer(words):
    if len(words) == 1 and words[0].isascii() and words[0].isdigit():
        return words[0] if len(words[0]) <= 12 else None
    if len(words) >= 2 and words[0] in _SMALL and words[1] == "hundred":
        hundreds = _SMALL[words[0]]
        if not 1 <= hundreds <= 9:
            return None
        tail = words[2:]
        if tail and tail[0] == "and":
            tail = tail[1:]
            if not tail:
                return None
        rest = _under_hundred(tail) if tail else 0
        return str(hundreds * 100 + rest) if rest is not None else None
    value = _under_hundred(words)
    return str(value) if value is not None else None


def _number(words):
    negative = bool(words and words[0] in {"negative", "minus", "-"})
    if negative:
        words = words[1:]
    if not words:
        return None
    if len(words) == 1 and re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)", words[0]):
        number = words[0]
        if number.startswith("."):
            number = "0" + number
    elif "point" in words or "." in words:
        positions = [index for index, value in enumerate(words) if value in {"point", "."}]
        if len(positions) != 1:
            return None
        point = positions[0]
        whole = _integer(words[:point]) if point else "0"
        digits = []
        for value in words[point + 1:]:
            if value in _SMALL and _SMALL[value] < 10:
                digits.append(str(_SMALL[value]))
            elif value.isascii() and value.isdigit():
                digits.append(value)
            else:
                return None
        if whole is None or not digits:
            return None
        number = whole + "." + "".join(digits)
    else:
        number = _integer(words)
    if number is None or len(number.replace(".", "")) > 12:
        return None
    return tuple(number) + (("negate",) if negative else ())


def keypad_sequence(expression: str) -> tuple[str, ...] | None:
    """Return operand/operator keys (not clear/equals), or refuse the whole input."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 160:
        return None
    text = expression.strip().casefold().replace("×", "*").replace("÷", "/").replace("−", "-")
    # A single sentence terminator is speech punctuation, not a math token.
    if text.endswith((".", "!", "?")):
        text = text[:-1].rstrip()
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    words = _TOKEN.findall(text)
    if not words or "".join(words) != re.sub(r"\s+", "", text):
        return None
    candidates = []
    for start in range(1, len(words) - 1):
        for width in (1, 2):
            operator = _OPERATORS.get(" ".join(words[start:start + width]))
            if operator is None:
                continue
            left, right = _number(words[:start]), _number(words[start + width:])
            if left is not None and right is not None:
                candidates.append(left + (operator,) + right)
    return candidates[0] if len(candidates) == 1 else None
