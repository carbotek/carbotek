# pyright: strict, reportPrivateUsage=false
"""Number formatters with report-local locale data and no global numeric context."""

import json
import math
import re
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from functools import lru_cache
from math import ceil as math_ceil
from math import floor as math_floor
from pathlib import Path
from typing import Any, cast

from carbotek.util import locale_key

from .condition import JS_UNDEFINED, js_truthy
from .jsnumber import (
    js_float_string,
    js_parse_float,
    js_parse_int,
    js_round,
    js_to_fixed,
    js_to_int32,
    js_to_number,
    js_to_string,
)
from .registry import FormatterMetadata, FormatterRegistry, MarkerContext

_BIG_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?", re.IGNORECASE)


def register(registry: FormatterRegistry) -> None:
    """Register deterministic arithmetic and basic number formatting."""
    math_metadata = FormatterMetadata(accepts_math=True, uses_render_options=True)
    registry.register("add", add, math_metadata)
    registry.register("sub", sub, math_metadata)
    registry.register("mul", mul, math_metadata)
    registry.register("div", div, math_metadata)
    registry.register("mod", mod)
    registry.register("abs", absolute)
    registry.register("ceil", ceiling)
    registry.register("floor", flooring)
    registry.register("round", round_number)
    context_metadata = FormatterMetadata(uses_render_options=True)
    registry.register("formatN", format_number, context_metadata)
    currency_metadata = FormatterMetadata(uses_render_options=True, uses_marker_context=True)
    registry.register("convCurr", convert_currency, currency_metadata)
    registry.register("formatC", format_currency, currency_metadata)
    registry.register("int", integer)
    registry.register("toFixed", to_fixed)
    registry.register("toEN", to_english)
    registry.register("toFR", to_french)
    registry.register("cid", identity)


def add(value: Any, operand: Any, *, render_options: Any = None) -> Any:
    return _arithmetic(value, operand, "add", render_options)


def sub(value: Any, operand: Any, *, render_options: Any = None) -> Any:
    return _arithmetic(value, operand, "sub", render_options)


def mul(value: Any, operand: Any, *, render_options: Any = None) -> Any:
    return _arithmetic(value, operand, "mul", render_options)


def div(value: Any, operand: Any, *, render_options: Any = None) -> Any:
    return _arithmetic(value, operand, "div", render_options)


def mod(value: Any, operand: Any) -> Any:
    if value is None or value is JS_UNDEFINED:
        return value
    try:
        return math.fmod(_js_parse_float(value), _js_parse_float(operand))
    except ValueError:
        return float("nan")


def _arithmetic(value: Any, operand: Any, operation: str, options: Any) -> Any:
    if value is None or value is JS_UNDEFINED:
        return value
    if getattr(options, "use_high_precision_arithmetic", False):
        try:
            left, right = _decimal(value), _decimal(operand)
            if right == 0 and operation == "div":
                return float("nan")
            return _decimal_operation(left, right, operation)
        except InvalidOperation, ValueError, ZeroDivisionError:
            return float("nan")
    left, right = _js_parse_float(value), _js_parse_float(operand)
    if operation == "add":
        return left + right
    if operation == "sub":
        return left - right
    if operation == "mul":
        return left * right
    return left / right if right != 0 else value


def absolute(value: Any) -> float | Any:
    return value if value is None or value is JS_UNDEFINED else abs(_js_parse_float(value))


def ceiling(value: Any) -> int | float | Any:
    number = _js_parse_float(value)
    return value if value is None or value is JS_UNDEFINED else math_ceil(number) if math.isfinite(number) else number


def flooring(value: Any) -> int | float | Any:
    number = _js_parse_float(value)
    return value if value is None or value is JS_UNDEFINED else math_floor(number) if math.isfinite(number) else number


def round_number(value: Any, precision: Any = JS_UNDEFINED) -> float:
    """``round(num, precision)``: a decimal shift of ``String(num)`` (no precision gives NaN, as in JS)."""
    return js_round(value, precision)


def format_number(value: Any, precision: Any = JS_UNDEFINED, *, render_options: Any = None) -> str | Any:
    """``formatN``: ``round()`` then ``toFixed()``, localized with the JS group regular expression."""
    if value is None or value is JS_UNDEFINED:
        return value
    language = locale_key(str(getattr(render_options, "lang", "en")), _LOCALE_NUMBERS)
    number_format = _LOCALE_NUMBERS.get(language, _LOCALE_NUMBERS["en"])
    high_precision = getattr(render_options, "use_high_precision_arithmetic", False) is True
    return _js_format(value, number_format, 3 if precision is JS_UNDEFINED else precision, high_precision)


def _js_format(value: Any, number_format: Mapping[str, Any], precision: Any, high_precision: bool) -> str:
    """``_format(value, format, precision, highPrecision)`` of carbone formatters/number.js."""
    if js_to_number(precision) > 40:
        raise ValueError("The number of decimals cannot be greater than 40")
    places = max(0, js_to_int32(js_to_number(precision)))
    if not high_precision:
        fixed = js_to_fixed(js_round(value, precision), places)
    elif isinstance(value, Decimal):  # a Big.js result: toFixed rounds half up (Big.RM = 1)
        with localcontext(Context()) as context:
            context.prec = max(len(value.as_tuple().digits), value.adjusted() + places + 1) + 2
            fixed = f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):.{places}f}"
    elif isinstance(value, int | float) and not isinstance(value, bool):
        fixed = js_to_fixed(js_to_number(value), places)
    else:
        raise TypeError("_value.toFixed is not a function")
    decimal = number_format.get("decimal")
    localized = fixed.replace(".", decimal, 1) if decimal else fixed
    separator = number_format.get("separator") or ","
    return _group_pattern(int(number_format["group"]), js_to_number(precision) > 0).sub(
        lambda match: match.group(0) + separator, localized
    )


@lru_cache(maxsize=512)
def _group_pattern(group: int, decimals: bool) -> re.Pattern[str]:
    """``\\d(?=(\\d{group})+\\D)`` with decimals, ``\\d(?=(\\d{group})+$)`` without (JS ``_re``)."""
    lookahead = r"\D" if decimals else "$"
    return re.compile(rf"[0-9](?=(?:[0-9]{{{group}}})+{lookahead})", re.ASCII)


def convert_currency(
    value: Any,
    target: Any = None,
    source: Any = None,
    *,
    render_options: Any = None,
    marker_context: MarkerContext | None = None,
) -> Any:
    """``convCurr``: ``(d / rates[source]) * rates[target]``; no source currency leaves the value intact."""
    currency = cast(Mapping[str, Any], getattr(render_options, "currency", None) or {})
    target = target if js_truthy(target) else currency.get("target")
    source = source if js_truthy(source) else currency.get("source")
    if not js_truthy(source):
        return value
    rates = cast(Mapping[str, Any], currency.get("rates") or {})
    target_rate = rates.get(target)
    source_rate = rates.get(source)
    target_rate = target_rate if js_truthy(target_rate) else 1
    source_rate = source_rate if js_truthy(source_rate) else 1
    if marker_context is not None:
        marker_context.modified_currency_target = target
    if getattr(render_options, "use_high_precision_arithmetic", False):
        if not isinstance(value, Decimal):
            if value is None or value is JS_UNDEFINED:
                kind = "null" if value is None else "undefined"
                raise TypeError(f"Cannot read properties of {kind} (reading 'div')")
            raise TypeError("d.div is not a function")
        return _decimal_operation(
            _decimal_operation(_decimal(value), _decimal(source_rate), "div"), _decimal(target_rate), "mul"
        )
    numerator = js_to_number(float(value) if isinstance(value, Decimal) else value)
    denominator = js_to_number(float(source_rate) if isinstance(source_rate, Decimal) else source_rate)
    if denominator == 0:
        quotient = (
            math.nan
            if numerator == 0 or math.isnan(numerator)
            else math.copysign(math.inf, numerator) * math.copysign(1, denominator)
        )
    else:
        quotient = numerator / denominator
    return quotient * js_to_number(float(target_rate) if isinstance(target_rate, Decimal) else target_rate)


def format_currency(
    value: Any,
    precision_or_format: Any = None,
    target: Any = None,
    *,
    render_options: Any = None,
    marker_context: MarkerContext | None = None,
) -> str | Any:
    if value is None or value is JS_UNDEFINED:
        return value
    currency = getattr(render_options, "currency", {})
    language = locale_key(str(getattr(render_options, "lang", "en")), _LOCALE_CURRENCY)
    locale = _LOCALE_CURRENCY.get(language, _LOCALE_CURRENCY["en"])
    modified = marker_context.modified_currency_target if marker_context is not None else None
    code = str(target or modified or currency.get("target") or locale["code"])
    info = _CURRENCIES[code]
    match = re.match(r"^\s*[+-]?\d+", str(precision_or_format)) if precision_or_format is not None else None
    precision = _precision(match.group()) if match is not None else info["precision"]
    converted = convert_currency(value, target, render_options=render_options)
    if marker_context is not None:
        marker_context.modified_currency_target = None  # reset for the next use (JS)
    number = format_number(converted, precision, render_options=render_options)
    display = str(precision_or_format)
    format_name = locale["formats"].get(display, locale["formats"]["L"])
    major = info["major"] if js_to_number(value) == 1 else f"{info['major']}s"
    symbol = info["symbol"]
    if format_name == "Sv":
        return f"{symbol}{number}"
    if format_name == "S_v":
        return f"{symbol} {number}"
    if format_name == "v_M":
        return f"{number} {major}"
    if format_name == "M":
        return major
    return f"{number} {symbol}"


def integer(value: Any) -> float:
    """``int``: ``parseInt(d, 10)``."""
    return js_parse_int(value)


def to_fixed(value: Any, precision: Any = JS_UNDEFINED) -> str:
    """``toFixed``: ``parseFloat(d).toFixed(nb)``."""
    number = js_parse_float(value)
    digits = 0.0 if precision is JS_UNDEFINED else js_to_number(precision)
    if math.isnan(digits):
        digits = 0.0
    elif math.isfinite(digits):
        digits = float(math.trunc(digits))
    if not 0 <= digits <= 100:
        raise ValueError("toFixed() digits argument must be between 0 and 100")
    return js_to_fixed(number, int(digits))


def to_english(value: Any) -> str:
    return number_string(value).replace(",", ".")


def to_french(value: Any) -> str:
    return number_string(value).replace(".", ",")


def number_string(value: Any) -> str:
    """``String(value)``, except that a Python ``int`` keeps all its digits (JS rounds beyond 2**53)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, Decimal):
        return decimal_string(value)
    return js_float_string(value) if isinstance(value, float) else js_to_string(value)


def decimal_string(value: Decimal) -> str:
    """Big.js coefficient printing without rounding through a binary float."""
    if not value.is_finite():
        return str(value)
    if value == 0:
        return "0"
    digits = "".join(map(str, value.as_tuple().digits)).rstrip("0")
    power = value.adjusted()
    sign = "-" if value.is_signed() else ""
    if power <= -7 or power >= 21:
        coefficient = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{coefficient}e{'+' if power >= 0 else '-'}{abs(power)}"
    point = power + 1
    if point <= 0:
        return sign + "0." + "0" * -point + digits
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return sign + digits[:point] + "." + digits[point:]


def _js_parse_float(value: Any) -> float:
    # parseFloat(String(x)) is x itself for a number (NaN and the infinities included).
    if type(value) is float:
        return value
    if type(value) is int and abs(value) < 2**1000:
        return float(value)
    match = re.match(r"^\s*[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", number_string(value))
    return float(match.group()) if match else float("nan")


def identity(value: Any) -> Any:
    return value


def _decimal(value: Any) -> Decimal:
    try:
        text = number_string(value)
        if _BIG_NUMBER.fullmatch(text) is None:
            raise ValueError("[big.js] Invalid number")
        number = Decimal(text)
        if not number.is_finite():
            raise ValueError("[big.js] Invalid number")
        return number
    except (InvalidOperation, ValueError) as error:
        raise ValueError("[big.js] Invalid number") from error


def _decimal_operation(left: Decimal, right: Decimal, operation: str) -> Decimal:
    """Big.js uses exact add/mul and twenty decimal places for division."""
    with localcontext(Context()) as context:
        context.prec = (
            len(left.as_tuple().digits)
            + len(right.as_tuple().digits)
            + abs(left.adjusted())
            + abs(right.adjusted())
            + abs(int(left.as_tuple().exponent))
            + abs(int(right.as_tuple().exponent))
            + 42
        )
        context.rounding = ROUND_HALF_UP
        if operation == "add":
            return left + right
        if operation == "sub":
            return left - right
        if operation == "mul":
            return left * right
        if right == 0:
            raise ValueError("[big.js] Division by zero")
        return (left / right).quantize(Decimal("1e-20")).normalize()


def _precision(value: Any) -> int:
    precision = int(value)
    if not 0 <= precision <= 40:
        raise ValueError("The number of decimals must be between 0 and 40")
    return precision


_CURRENCIES: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("currencies.json").read_text(encoding="utf-8")
)
_LOCALE_CURRENCY: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("locale_currency.json").read_text(encoding="utf-8")
)

_LOCALE_NUMBERS: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("locale_numbers.json").read_text(encoding="utf-8")
)
