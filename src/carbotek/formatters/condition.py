# pyright: strict, reportPrivateUsage=false
"""Public condition formatter surface shared with the marker executor."""

import math
import re
from collections.abc import Mapping
from typing import Any

from carbotek.typing_helpers import is_list, is_mapping, is_sequence

from .jsnumber import JS_UNDEFINED as JS_UNDEFINED  # re-exported: the formatters import it from here
from .jsnumber import js_float_string
from .registry import FormatterMetadata, FormatterRegistry, MarkerContext


def register(registry: FormatterRegistry) -> None:
    metadata = FormatterMetadata(is_conditional=True)
    for name, function in {
        "ifEM": if_empty,
        "ifNEM": if_not_empty,
        "ifEQ": if_equal,
        "ifNE": if_not_equal,
        "ifGT": if_greater,
        "ifGTE": if_greater_equal,
        "ifLT": if_less,
        "ifLTE": if_less_equal,
        "ifIN": if_in,
        "ifNIN": if_not_in,
        "ifTE": if_type_equal,
        "and": operator_value,
        "or": operator_value,
    }.items():
        registry.register(name, function, metadata)
    propagation = FormatterMetadata(is_conditional=True, updates_propagation=True)
    registry.register("ifContain", if_contain, propagation)
    registry.register("ifEqual", if_equal, propagation)
    state = FormatterMetadata(is_conditional=True, uses_marker_context=True, updates_propagation=True)
    registry.register("show", show, state)
    registry.register("elseShow", else_show, state)
    registry.register("showBegin", show_begin, state)
    registry.register("hideBegin", hide_begin, state)
    block = FormatterMetadata(is_conditional=True, uses_marker_context=True)
    registry.register("showEnd", block_end, block)
    registry.register("hideEnd", block_end, block)


def if_contain(value: Any, expected: Any) -> bool:
    return _includes(value, expected)


def if_equal(value: Any, expected: Any) -> bool:
    return _js_string(value) == _js_string(expected)


def if_not_equal(value: Any, expected: Any) -> bool:
    return not if_equal(value, expected)


def if_greater(value: Any, expected: Any) -> bool:
    return _parse_float(value) > _parse_float(expected)


def if_greater_equal(value: Any, expected: Any) -> bool:
    return _parse_float(value) >= _parse_float(expected)


def if_less(value: Any, expected: Any) -> bool:
    return _parse_float(value) < _parse_float(expected)


def if_less_equal(value: Any, expected: Any) -> bool:
    return _parse_float(value) <= _parse_float(expected)


def if_in(value: Any, expected: Any) -> bool:
    return js_truthy(expected) and _includes(value, expected)


def if_not_in(value: Any, expected: Any) -> bool:
    return not _includes(value, expected)


def if_empty(value: Any) -> bool:
    return (
        value is JS_UNDEFINED
        or value is None
        or value == ""
        or value == []
        or value == {}
        or (isinstance(value, float) and math.isnan(value))
    )


def if_not_empty(value: Any) -> bool:
    return not if_empty(value)


def if_type_equal(value: Any, expected: Any) -> bool:
    return {
        "array": is_list(value),
        "object": is_mapping(value),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "number": _is_number(value) and not (isinstance(value, float) and math.isnan(value)),
        "integer": _is_number(value) and math.isfinite(value) and value % 1 == 0,
        "binary": isinstance(value, bool) or _js_string(value) in {"true", "false", "0", "1"},
    }.get(expected if isinstance(expected, str) else "", False)


_FLOAT_PREFIX = re.compile(r"^[\s]*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _js_string(value: Any) -> str:
    if value is JS_UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return js_float_string(value)
    if is_list(value):
        return ",".join("" if item is None or item is JS_UNDEFINED else _js_string(item) for item in value)
    if is_mapping(value):
        return "[object Object]"
    return str(value)


def _parse_float(value: Any) -> float:
    match = _FLOAT_PREFIX.match(_js_string(value))
    return float(match.group(1)) if match else math.nan


def js_truthy(value: Any) -> bool:
    if value is JS_UNDEFINED or value is None or value is False or value == "":
        return False
    return not (_is_number(value) and (value == 0 or (isinstance(value, float) and math.isnan(value))))


def _includes(value: Any, expected: Any) -> bool:
    if isinstance(value, str):
        return _js_string(expected) in value
    if is_sequence(value):
        return any(_js_strict_equal(item, expected) for item in value)
    return False


def _js_strict_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Mapping | list | tuple) or isinstance(right, Mapping | list | tuple):
        return left is right
    if _is_number(left) and _is_number(right):
        return left == right
    return type(left) is type(right) and left == right


def hidden_marker(_value: Any, *_arguments: Any) -> str:
    return ""


def passthrough(value: Any, *_arguments: Any) -> Any:
    return value


def _state(context: MarkerContext | None) -> MarkerContext:
    return context if context is not None else MarkerContext(data={}, complement={})


def _condition(context: MarkerContext, value: Any, expected: bool) -> bool:
    """``isConditionTrue === expected || (isConditionTrue === null && truthy(d) === expected)``."""
    state = context.is_condition_true
    return state is expected or (state is None and js_truthy(value) is expected)


def show(value: Any, message: Any = JS_UNDEFINED, *, marker_context: MarkerContext | None = None) -> Any:
    context = _state(marker_context)
    if _condition(context, value, True):
        context.stop_propagation = True
        return message
    return value


def else_show(value: Any, message: Any = JS_UNDEFINED, *, marker_context: MarkerContext | None = None) -> Any:
    context = _state(marker_context)
    if _condition(context, value, False):
        context.stop_propagation = True
        return message
    return value


def show_begin(value: Any, *, marker_context: MarkerContext | None = None) -> str:
    context = _state(marker_context)
    context.hidden_flag = 1
    if _condition(context, value, True):
        context.stop_propagation = True
        context.hidden_flag = 0
    return ""


def hide_begin(value: Any, *, marker_context: MarkerContext | None = None) -> str:
    context = _state(marker_context)
    context.hidden_flag = 0
    if _condition(context, value, True):
        context.stop_propagation = True
        context.hidden_flag = 1
    return ""


def block_end(value: Any, *, marker_context: MarkerContext | None = None) -> str:
    del value
    _state(marker_context).hidden_flag = -1
    return ""


def operator_value(value: Any, *arguments: Any) -> Any:
    """``and``/``or``: ``arguments.length === 1 ? d : value`` (the operator itself is the renderer's)."""
    return arguments[0] if arguments else value
