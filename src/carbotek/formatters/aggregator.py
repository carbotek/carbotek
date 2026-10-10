# pyright: strict, reportPrivateUsage=false
"""Pure collection aggregators used by the document renderer.

The functions deliberately accept only collection values.  Path traversal is a
renderer responsibility, which keeps this module independent of OOXML and of
the current render context.
"""

import math
import re
from decimal import Decimal
from typing import Any

from carbotek.typing_helpers import is_list, is_sequence

from .condition import JS_UNDEFINED
from .number import number_string
from .registry import FormatterMetadata, FormatterRegistry, MarkerContext

_MAX_SAFE_INTEGER = 9007199254740991


class _AggregatedStrings(list[Any]):
    def __init__(self, values: list[Any] | None = None) -> None:
        super().__init__(values or [])
        self.is_reversed = False


def register(registry: FormatterRegistry) -> None:
    """Register aggregators which do not need rendering state."""
    metadata = FormatterMetadata(uses_marker_context=True)
    registry.register("aggSum", agg_sum, metadata)
    registry.register("aggSumGet", agg_sum_get, metadata)
    registry.register("aggAvg", agg_avg, metadata)
    registry.register("aggAvgGet", agg_avg_get, metadata)
    registry.register("aggMin", agg_min, metadata)
    registry.register("aggMinGet", agg_min_get, metadata)
    registry.register("aggMax", agg_max, metadata)
    registry.register("aggMaxGet", agg_max_get, metadata)
    registry.register("aggCount", agg_count, metadata)
    registry.register("aggCountGet", agg_count_get, metadata)
    registry.register("aggCountD", agg_count_distinct, metadata)
    registry.register("aggCountDGet", agg_count_distinct_get, metadata)
    registry.register("aggStr", agg_str, metadata)
    registry.register("aggStrGet", agg_str_get, metadata)
    registry.register("aggStrGetReverse", agg_str_get_reverse, metadata)
    registry.register("aggStrD", agg_str_distinct, metadata)
    registry.register("aggStrDGet", agg_str_distinct_get, metadata)
    registry.register("aggStrDGetReverse", agg_str_distinct_get_reverse, metadata)
    registry.register("cumSum", cum_sum, metadata)
    registry.register("cumAvg", cum_avg, metadata)
    registry.register("cumCount", cum_count, metadata)
    registry.register("cumCountD", cum_count_distinct, metadata)


def agg_sum(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> float | int:
    """Sum numeric members; an empty collection has the Carbotek value zero."""
    if _is_collection(value):
        numbers = [parse_float_js(0 if _is_nullish(item) else item) for item in _items(value)]
        return _js_sum(numbers, marker_context)
    key = _key(marker_context, groups)
    total = _state(marker_context).get(key, 0) + parse_float_js(0 if _is_nullish(value) else value)
    _state(marker_context)[key] = total
    return total


def agg_sum_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> float | int | Decimal:
    if _is_collection(value):
        return agg_sum(value)
    return _state(marker_context).get(_key(marker_context, groups), Decimal())


def agg_avg(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> float | int:
    """Return the arithmetic mean, or zero when the collection is empty."""
    if _is_collection(value):
        numbers = [parse_float_js(0 if _is_nullish(item) else item) for item in _items(value)]
        return _js_sum(numbers, marker_context) / len(numbers) if numbers else 0
    key = _key(marker_context, groups)
    total, count = _state(marker_context).get(key, (0, 0))
    total, count = total + parse_float_js(0 if _is_nullish(value) else value), count + 1
    _state(marker_context)[key] = (total, count)
    return total / count


def agg_avg_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> float | int | Decimal:
    if _is_collection(value):
        return agg_avg(value)
    total, count = _state(marker_context).get(_key(marker_context, groups), (Decimal(), 0))
    return total / count if count else Decimal()


def agg_min(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    """Return the smallest valid numeric member, or zero if none exists."""
    if _is_collection(value):
        numbers = [parse_float_js(item) for item in _items(value) if not _is_nullish(item)]
        valid = [number for number in numbers if not math.isnan(number)]
        return min(valid, default=0)
    key = _key(marker_context, groups)
    previous = _state(marker_context).get(key, _MAX_SAFE_INTEGER)
    number = parse_float_js(_MAX_SAFE_INTEGER if _is_nullish(value) else value)
    if number < previous:
        _state(marker_context)[key] = number
        return number
    return previous


def agg_min_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    return (
        agg_min(value) if _is_collection(value) else _state(marker_context).get(_key(marker_context, groups), Decimal())
    )


def agg_max(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    """Return the largest valid numeric member, or zero if none exists."""
    if _is_collection(value):
        numbers = [parse_float_js(item) for item in _items(value) if not _is_nullish(item)]
        valid = [number for number in numbers if not math.isnan(number)]
        return max(valid, default=0)
    key = _key(marker_context, groups)
    previous = _state(marker_context).get(key, -_MAX_SAFE_INTEGER)
    number = parse_float_js(-_MAX_SAFE_INTEGER if _is_nullish(value) else value)
    if number > previous:
        _state(marker_context)[key] = number
    return None


def agg_max_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    return (
        agg_max(value) if _is_collection(value) else _state(marker_context).get(_key(marker_context, groups), Decimal())
    )


def agg_count(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> int:
    """Count members, including null values."""
    if _is_collection(value):
        return len(_items(value))
    key = _key(marker_context, groups)
    result = int(_state(marker_context).get(key, 0)) + 1
    _state(marker_context)[key] = result
    return result


def agg_count_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> int:
    return (
        agg_count(value) if _is_collection(value) else int(_state(marker_context).get(_key(marker_context, groups), 0))
    )


def agg_count_distinct(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> int:
    """Count distinct non-null scalar members, retaining insertion semantics."""
    if _is_collection(value):
        return len(_distinct(item for item in _items(value) if item is not None))
    key = _key(marker_context, groups)
    values = _state(marker_context).setdefault(key, [])
    if not _is_nullish(value) and value not in values:
        values.append(value)
    return len(values)


def agg_count_distinct_get(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> int:
    return (
        agg_count_distinct(value)
        if _is_collection(value)
        else len(_state(marker_context).get(_key(marker_context, groups), []))
    )


def agg_str(value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    """Join non-null members in source order."""
    if _is_collection(value):
        return js_join((item for item in _items(value) if not _is_nullish(item)), separator)
    key = _key(marker_context, groups)
    values = _state(marker_context).get(key, [])
    if not _is_nullish(value):
        values.append(value)
        _state(marker_context)[key] = values
    return values


def agg_str_get(value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None) -> str:
    return js_join(_state(marker_context).get(_key(marker_context, groups), []), separator)


def agg_str_get_reverse(
    value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None
) -> str:
    key = _key(marker_context, groups)
    values = _state(marker_context).get(key)
    if values is None:
        return ""
    if not isinstance(values, _AggregatedStrings):
        values = _AggregatedStrings(values)
        _state(marker_context)[key] = values
    if not values.is_reversed:
        values.reverse()
        values.is_reversed = True
    return js_join(values, separator)


def agg_str_distinct(
    value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None
) -> Any:
    """Join distinct non-null members in the JS builder's traversal order."""
    if _is_collection(value):
        items = [item for item in _items(value) if not _is_nullish(item)]
        if marker_context is not None and not marker_context.aggregate_forward:
            # The JS builder collects from the end, then prints the Set in reverse order.
            return js_join(reversed(_distinct(reversed(items))), separator)
        return js_join(_distinct(items), separator)
    key = _key(marker_context, groups)
    values = _state(marker_context).get(key, [])
    if not _is_nullish(value) and not _js_contains(values, value):
        values.append(value)
        _state(marker_context)[key] = values
    return values


def agg_str_distinct_get(
    value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None
) -> str:
    return js_join(_state(marker_context).get(_key(marker_context, groups), []), separator)


def agg_str_distinct_get_reverse(
    value: Any, separator: Any = ", ", *groups: Any, marker_context: MarkerContext | None = None
) -> str:
    return js_join(reversed(_state(marker_context).get(_key(marker_context, groups), [])), separator)


def cum_sum(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    del groups, marker_context
    return value


def cum_avg(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    del groups, marker_context
    return value


def cum_count(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    del groups, marker_context
    return value


def cum_count_distinct(value: Any, *groups: Any, marker_context: MarkerContext | None = None) -> Any:
    del groups, marker_context
    return value


def _items(value: Any) -> list[Any]:
    return list(value) if is_sequence(value) else []


def _js_sum(numbers: list[float], context: MarkerContext | None) -> float | int:
    """``total += item`` in the order the JS loop visits the items (``sum()`` would compensate)."""
    total: float | int = 0
    for number in numbers if context is not None and context.aggregate_forward else reversed(numbers):
        total += number
    return total


def _is_collection(value: Any) -> bool:
    return isinstance(value, list | tuple)


def _state(context: MarkerContext | None) -> dict[str, Any]:
    return context.aggregation_database if context is not None else {}


def _key(context: MarkerContext | None, groups: tuple[Any, ...]) -> str:
    return f"{context.marker_key if context is not None else ''}_{js_join(groups, '_')}"


def _distinct(items: Any) -> list[Any]:
    result: list[Any] = []
    for item in items:
        if not _js_contains(result, item):
            result.append(item)
    return result


def _is_nullish(value: Any) -> bool:
    return value is None or value is JS_UNDEFINED


def parse_float_js(value: Any) -> float:
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is int:
        try:
            return float(value)
        except OverflowError:
            pass
    match = re.match(r"^\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))", str(value))
    return float(match.group(1)) if match else math.nan


def js_join(values: Any, separator: Any) -> str:
    delimiter = "null" if separator is None else _js_string(separator)
    return delimiter.join("" if item is None else _js_string(item) for item in values)


def _js_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "0" if value == 0.0 else number_string(value)
    if is_list(value):
        return ",".join("" if item is None else _js_string(item) for item in value)
    return "[object Object]" if isinstance(value, dict) else str(value)


def _js_contains(values: list[Any], value: Any) -> bool:
    return any(_js_same_value(item, value) for item in values)


def _js_same_value(left: Any, right: Any) -> bool:
    if isinstance(left, dict | list) or isinstance(right, dict | list):
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float | Decimal) and isinstance(right, int | float | Decimal):
        return left == right or (
            isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right)
        )
    return type(left) is type(right) and left == right


# Formatter rewrite of the JS builder (``fn.replacedBy``): an aggregator becomes itself, which
# postpones the rest of the chain, plus its ``Get`` counterpart. The tree renderer computes the
# same values directly (_prepare_aggregate); this port documents and tests the JS contract.
_STRING_AGGREGATORS = frozenset({"aggStr", "aggStrD"})
_CUMULATIVE = frozenset({"cumSum", "cumAvg", "cumCount", "cumCountD"})


def replaced_by(
    name: str, arguments: list[str], parents: int = 0, reverse: bool | None = None
) -> list[tuple[str, list[str], bool]]:
    """``(formatter, arguments, isNextFormatterExecutionPostponed)`` pairs replacing ``name``."""
    if name in _CUMULATIVE:
        return [(name, list(arguments), True), ("agg" + name.removeprefix("cum"), list(arguments), False)]
    remaining = list(arguments)
    separator: str | None = None
    if name in _STRING_AGGREGATORS:
        separator = remaining.pop(0) if remaining else ", "
    group = remaining if remaining or reverse is False else ["." * (level + 1) + "i" for level in range(parents)]
    first = [separator, *group] if separator is not None else list(group)
    getter = name + "Get" + ("Reverse" if separator is not None and reverse is True else "")
    # JS unshifts the separator into the argument array both formatters share.
    return [(name, first, True), (getter, list(first), False)]
