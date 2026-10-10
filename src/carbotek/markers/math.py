# pyright: strict, reportPrivateUsage=false
"""Arithmetic arguments of ``add``/``sub``/``mul``/``div`` (port of ``parser.parseMathematicalExpression``).

JS splits the argument on ``* + / -``, turns every operand that starts with a digit, a dot or
``d.``/``c.`` into ``parseFloat(<value>)`` and evaluates the resulting JavaScript expression. The port
builds the same operand/operator sequence and evaluates it with normal precedence, without
``eval``.
"""

import math
import re
from collections.abc import Callable
from typing import Any

from carbotek.errors import MarkerError

_OPERAND = re.compile(r"^[0-9.]|^d\.|^c\.")


class BadMathematicalExpression(MarkerError):
    """``Bad Mathematical Expression in "…"`` of lib/parser.js."""


def terms(expression: str) -> list[tuple[str, str]]:
    """``(operator, operand)`` pairs left to right; raise the JS error for an empty operand."""
    if not isinstance(expression, str) or not expression.strip():  # pyright: ignore[reportUnnecessaryIsInstance] - JS typeof check
        return []
    operands = re.split(r"[*+/\-]", expression)
    position = len(expression) - 1
    pending = ""
    previous = ""
    result: list[tuple[str, str]] = []
    for operand in reversed(operands):
        position -= len(operand) + 1
        operator = expression[position + 1] if 0 <= position + 1 < len(expression) else ""
        trimmed = operand.strip()
        if not trimmed and previous != "-":
            raise BadMathematicalExpression(f'Bad Mathematical Expression in "{expression}"')
        if _OPERAND.match(trimmed) is None:
            pending = operator + trimmed + pending
            continue
        result.append((operator, trimmed + pending))
        pending = ""
        previous = operator
    result.reverse()
    return result


def evaluate(expression: str, operand: Callable[[str], float]) -> float | str:
    """Value of the expression; ``operand`` returns ``parseFloat`` of one resolved operand."""
    pairs = terms(expression)
    if not pairs:
        return ""
    values: list[float] = []
    operators: list[str] = []
    for index, (operator, text) in enumerate(pairs):
        value = operand(text)
        if index == 0:
            values.append(-value if operator == "-" else value)
            continue
        operators.append(operator)
        values.append(value)
    # * and / bind tighter than + and -, left to right (JavaScript semantics).
    reduced = [values[0]]
    pending_ops: list[str] = []
    for operator, value in zip(operators, values[1:], strict=True):
        if operator == "*":
            reduced[-1] = reduced[-1] * value
        elif operator == "/":
            reduced[-1] = _divide(reduced[-1], value)
        else:
            pending_ops.append(operator)
            reduced.append(value)
    total = reduced[0]
    for operator, value in zip(pending_ops, reduced[1:], strict=True):
        total = total + value if operator == "+" else total - value
    return total


def _divide(left: float, right: float) -> float:
    if right == 0:
        if left == 0 or math.isnan(left):
            return math.nan
        return math.copysign(math.inf, left) * math.copysign(1.0, right)
    return left / right


def is_math_candidate(argument: Any) -> bool:
    return isinstance(argument, str) and bool(re.search(r"[*+/\-]", argument))
