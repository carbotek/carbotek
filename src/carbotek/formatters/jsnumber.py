# pyright: strict
"""JavaScript number semantics shared by the formatters.

ECMA-262 ``Number::toString`` and ``StringToNumber``, ``Math.round``, ``Number.prototype.toFixed``
and the string concatenation of ``+``, for JS values in their Python form (``None`` for null,
``JS_UNDEFINED`` for undefined, ``int`` for a JSON integer, which JS holds as the nearest double).
"""

import math
import re
from typing import Any

from carbotek.typing_helpers import is_dict, is_sequence

# The JS undefined value (a missing argument or field); re-exported by condition.py.
JS_UNDEFINED: Any = object()

# StrWhiteSpaceChar: TAB VT FF ZWNBSP, the Zs category, and the line terminators LF CR LS PS.
_JS_SPACE = "\t\n\v\f\r                  　﻿"
_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)", re.ASCII)
_NON_DECIMAL = re.compile(r"0(?:[xX]([0-9a-fA-F]+)|[oO]([0-7]+)|[bB]([01]+))", re.ASCII)


def js_float_string(value: float) -> str:
    """``String(x)``: the shortest round-trip digits, in exponent form below 1e-6 and from 1e21."""
    if value != value:
        return "NaN"
    if value == 0:
        return "0"  # +0 and -0
    if value < 0:
        return "-" + js_float_string(-value)
    if value == math.inf:
        return "Infinity"
    text = repr(value)  # the same shortest digits (David Gay's dtoa, mode 0)
    if "e" not in text:  # repr is plain exactly for 1e-4 <= x < 1e16, where JS is plain too
        return text.removesuffix(".0")
    mantissa, _, exponent = text.partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).rstrip("0")
    count = len(digits)
    point = int(exponent) + 1  # x = 0.digits * 10**point
    if count <= point <= 21:
        return digits + "0" * (point - count)
    if 0 < point <= 21:
        return f"{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    power = point - 1
    sign = "+" if power >= 0 else "-"
    head = digits if count == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{head}e{sign}{abs(power)}"


def js_number(value: int) -> float:
    """The double JS holds for a JSON integer (correctly rounded, ties to even)."""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def js_to_string(value: Any) -> str:
    """ToString of a JS value (``value + ''``)."""
    if value is None:
        return "null"
    if value is JS_UNDEFINED:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return js_float_string(value)
    if isinstance(value, int):
        return js_float_string(js_number(value))
    if isinstance(value, str):
        return value
    if is_sequence(value):
        return ",".join("" if item is None or item is JS_UNDEFINED else js_to_string(item) for item in value)
    if is_dict(value):
        return "[object Object]"
    return str(value)


def js_string_to_number(text: str) -> float:
    """``Number(string)``: trimmed decimal, Infinity, 0x/0o/0b integers, empty is 0, else NaN."""
    text = text.strip(_JS_SPACE)
    if not text:
        return 0.0
    if _DECIMAL.fullmatch(text):
        if text.endswith("Infinity"):
            return -math.inf if text[0] == "-" else math.inf
        return float(text)
    match = _NON_DECIMAL.fullmatch(text)
    if match is None:
        return math.nan
    hexadecimal, octal, binary = match.groups()
    digits, base = (hexadecimal, 16) if hexadecimal else (octal, 8) if octal else (binary, 2)
    return js_number(int(digits, base))


def js_to_number(value: Any) -> float:
    """ToNumber of a JS value."""
    if value is None:
        return 0.0
    if value is JS_UNDEFINED:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return js_number(value)
    if isinstance(value, str) or is_sequence(value) or is_dict(value):
        return js_string_to_number(js_to_string(value))
    return math.nan


def _is_string_like(value: Any) -> bool:
    return isinstance(value, str) or is_sequence(value) or is_dict(value)


def js_add(left: Any, right: Any) -> Any:
    """The ``+`` operator: string concatenation as soon as a side is a string, array or object."""
    if _is_string_like(left) or _is_string_like(right):
        return js_to_string(left) + js_to_string(right)
    return js_to_number(left) + js_to_number(right)


def js_math_round(value: float) -> float:
    """``Math.round``: the nearest integer, ties toward +Infinity (exact, unlike ``floor(x + 0.5)``)."""
    if not math.isfinite(value) or value == 0:
        return value
    if -0.5 <= value < 0:
        return -0.0
    floor = math.floor(value)
    return float(floor + 1) if value - floor >= 0.5 else float(floor)  # value - floor is exact


def js_to_int32(value: float) -> int:
    """ToInt32, as the ``~~`` operator applies it."""
    if not math.isfinite(value):
        return 0
    number = int(value) % 2**32
    return number - 2**32 if number >= 2**31 else number


def js_to_fixed(value: float, digits: int) -> str:
    """``Number.prototype.toFixed``: the exact double rounded half away from zero."""
    if value != value:
        return "NaN"
    if math.isinf(value) or abs(value) >= 1e21:
        return js_float_string(value)
    sign = "-" if value < 0 else ""  # (-0).toFixed(2) is "0.00", (-0.001).toFixed(2) is "-0.00"
    numerator, denominator = abs(value).as_integer_ratio()
    rounded, remainder = divmod(numerator * 10**digits, denominator)
    if remainder * 2 >= denominator:
        rounded += 1
    text = str(rounded)
    if digits:
        text = text.zfill(digits + 1)
        text = f"{text[:-digits]}.{text[-digits:]}"
    return sign + text


def js_round(number: Any, precision: Any) -> float:
    """``round(num, precision)`` of carbone formatters/number.js: a decimal shift of ``String(num)``.

    ``precision`` is the JS value the template passes (usually a string): in the exponent branch
    ``Number(exponent) + precision`` concatenates a string precision, as JS does.
    """
    text = js_to_string(number)
    places = js_to_string(precision)
    if "e" not in text:
        rounded = js_math_round(js_string_to_number(f"{text}e+{places}"))
        return js_string_to_number(f"{js_float_string(rounded)}e-{places}")
    parts = text.split("e")  # "undefined" has two
    mantissa, exponent = parts[0], parts[1]
    shifted = js_add(js_string_to_number(exponent), precision)
    sign = "+" if js_to_number(shifted) > 0 else ""
    head = f"{js_float_string(js_string_to_number(mantissa))}e{sign}{js_to_string(shifted)}"
    rounded = js_math_round(js_string_to_number(head))
    return js_string_to_number(f"{js_float_string(rounded)}e-{places}")


def js_parse_float(value: Any) -> float:
    """``parseFloat(String(value))``: the longest decimal prefix after leading white space."""
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value  # parseFloat(String(-0)) is +0
    match = _DECIMAL.match(js_to_string(value).lstrip(_JS_SPACE))
    if match is None:
        return math.nan
    literal = match.group(0)
    if literal.endswith("Infinity"):
        return -math.inf if literal[0] == "-" else math.inf
    return float(literal)


def js_parse_int(value: Any) -> float:
    """``parseInt(String(value), 10)``: ASCII digits after leading white space and a sign."""
    match = re.match(r"([+-]?)([0-9]+)", js_to_string(value).lstrip(_JS_SPACE), re.ASCII)
    if match is None:
        return math.nan
    number = js_number(int(match.group(1) + match.group(2)))
    return -0.0 if number == 0 and match.group(1) == "-" else number
