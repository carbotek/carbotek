# pyright: strict, reportPrivateUsage=false
"""Array formatters without OOXML or rendering-state dependencies."""

import re
from collections.abc import Mapping
from typing import Any

from carbotek.typing_helpers import is_list, is_mapping

from .jsnumber import js_float_string
from .registry import FormatterRegistry

_MISSING = object()


def register(registry: FormatterRegistry) -> None:
    """Register portable array operations."""
    registry.register("count", count)
    registry.register("arrayJoin", array_join)
    registry.register("arrayMap", array_map)


def count(value: Any) -> str:
    del value
    return ""


def array_join(value: Any, separator: Any = ", ", start: Any = 0, size: Any = _MISSING) -> Any:
    if not is_list(value):
        return value
    separator = _line_break_separator(separator)
    first = _parse_int(start)
    if size is _MISSING:
        sliced = value[first:]
    else:
        length = 0 if size is None else _parse_int(size) if isinstance(size, str) else int(size)
        sliced = value[first : first + length if length > 0 else length]
    return _js_string(separator).join("" if item is None else _js_string(item) for item in sliced)


def array_map(value: Any, object_separator: Any = ", ", attribute_separator: Any = ":", *attributes: Any) -> Any:
    """Flatten scalar fields of collection members in JavaScript property order."""
    if not is_list(value):
        return value
    mapped: list[str] = []
    for item in value:
        if attributes:
            flattened = [_property(item, field) for field in attributes]
        elif is_mapping(item):
            flattened = [member for member in item.values() if not isinstance(member, Mapping | list | tuple)]
        elif is_list(item):
            flattened = [member for member in item if not isinstance(member, Mapping | list | tuple)]
        else:
            flattened = [item]
        mapped.append(
            _js_string(attribute_separator).join("" if field is None else _js_string(field) for field in flattened)
        )
    return _js_string(object_separator).join(mapped)


def _line_break_separator(value: Any) -> str:
    if value == r"\n":
        return "\n"
    if value == r"\r\n":
        return "\r\n"
    return _js_string(value)


def _parse_int(value: Any) -> int:
    match = re.match(r"^\s*([+-]?\d+)", str(value))
    return int(match.group(1)) if match else 0


def _property(value: Any, key: Any) -> Any:
    if is_mapping(value):
        return value.get(str(key))
    if isinstance(value, str) or is_list(value):
        index = _parse_int(key)
        if str(index) == str(key) and 0 <= index < len(value):
            return value[index]
    return None


def _js_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return js_float_string(value)
    if is_list(value):
        return ",".join("" if item is None else _js_string(item) for item in value)
    if is_mapping(value):
        return "[object Object]"
    return "null" if value is None else str(value)
