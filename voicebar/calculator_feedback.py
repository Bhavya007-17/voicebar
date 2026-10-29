"""Compare a closed Calculator key sequence with an observed numeric display."""
from __future__ import annotations

import re
from decimal import Decimal, DecimalException, localcontext

_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?")


def expected_result(sequence):
    """Evaluate only the validated digit/sign/binary-operator sequence, never text."""
    operators = [index for index, token in enumerate(sequence) if token in {"+", "-", "*", "/"}]
    if len(operators) != 1:
        return None
    index = operators[0]

    def number(tokens):
        negative = tokens and tokens[-1] == "negate"
        digits = tokens[:-1] if negative else tokens
        value = "".join(digits)
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or len(value) > 13:
            return None
        return Decimal(("-" if negative else "") + value)

    try:
        left, right = number(sequence[:index]), number(sequence[index + 1:])
        if left is None or right is None:
            return None
        # Each input has at most twelve decimal digits. Eighty digits leave
        # sufficient guard digits for comparing the displayed rounded quotient.
        with localcontext() as context:
            context.prec = 80
            operator = sequence[index]
            if operator == "+":
                result = left + right
            elif operator == "-":
                result = left - right
            elif operator == "*":
                result = left * right
            else:
                result = left / right
        return result if result.is_finite() else None
    except DecimalException:
        return None


def display_number(name):
    """Accept the numeric form observed in the owned English Calculator probe."""
    if not isinstance(name, str) or len(name) > 200 or not name.startswith("Display is "):
        return None
    text = name[11:].strip().replace("−", "-")
    if _NUMBER.fullmatch(text) is None:
        return None
    try:
        value = Decimal(text.replace(",", ""))
        return (text, value) if value.is_finite() and abs(value.as_tuple().exponent) <= 100 else None
    except DecimalException:
        return None


def matches_result(expected, actual):
    if expected is None or actual is None:
        return False
    if expected == actual:
        return True
    # A coarse stale value (5 for expected 5.1) must not count as rounding.
    # Only a full-precision numeric display can match an inexact quotient.
    if len(actual.as_tuple().digits) < 16:
        return False
    try:
        with localcontext() as context:
            context.prec = 120
            return expected.quantize(actual) == actual
    except DecimalException:
        return False
